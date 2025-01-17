# -*- coding: utf-8 -*-
"""
| Author: Alexandre CARRE 
| Created on: Nov 23, 2020
"""
import copy
import logging
import os
import re
import shutil
from abc import ABC
from collections import OrderedDict
from typing import List, Tuple, Dict, Optional, Union, Sequence

import ants
import numpy as np
import torch
from HD_BET.run import run_hd_bet
from scipy import ndimage

from cBrainMRIPrePro import utils
from .utils.files import safe_file_name, split_filename, load_nifty_volume_as_array, save_to_nii
from .utils.image_processing import min_max_scaling, invert_min_max_scaling, get_mask, zscore_normalize

logger = logging.getLogger(__name__)


class DataPreprocessing(ABC):
    """
    The provided pipeline offer the possibility to resample image spacing, bias field correct, co-register
    (in MNI152 template or in a reference modality), skull-stripped, and z-score normalize.

    Class for data cBrainMRIPrePro input modalities as a dict:

        - Bias field correction of modalities (optional).
        - resampling (optional).
        - Co-registration: If template is set to True. Register to reference to template and then register other
          modalities to reference. If template is set to False. Register to reference.
        - Skull-stripped the reference modalities and apply the mask on others modalities.
        - Normalization Z-Score (optional).

    .. note:: Template is the MNI ICBM 2009a Nonlinear aSymmetric(referred to as ICBM152) stereotaxic template (as used in Yeh. et al. Nature, 2022, paper)
        It will add the suffix of the keys to data of each step. so if your keys is "T2" and values "/path/data_t1.nii.gz"
        it will split the values and look at the last suffix "t1", so will be data_step_t1_T2.nii.gz
        it will ignore case sensitivity, so if keys is "T1" and values "/path/data_t1.nii.gz", the step will be
        data_step_t1.nii.gz

    Args:
        dict_image: keys is the corresponding modality and value is path, ie {"T1": "t1_path", "T2": "t2_path"} ..
        reference: reference modality (used for co-registration and ss). if reference is not in dict_image,
            no cBrainMRIPrePro step is applied, except for coregistration.
        output_folder: output directory were to save cBrainMRIPrePro data
        reorient_image: str for Reorient an image. For possible reorientation see
            `<https://antspy.readthedocs.io/en/latest/_modules/ants/registration/reorient_image.html>`_
            default is None.
        resample_spacing: resolution for resampling (ie (1, 1, 1) in mm
        inter_type_resample: mode for resample 0 (Linear), 1 (NearestNeighbor), 2 (Gaussian), 3 (WindowedSinc),
            4 (BSpline), default: 4
        n4_correction: bias field correction. Specified as list corresponding to keys of dict_image
        do_coregistration: set to True to coregister the data.
        type_of_transform: type of transform for registration. (default: "Affine"). Can be of type:

            - "Translation": Translation transformation.
            - "Rigid": Rigid transformation: Only rotation and translation.
            - "Similarity": Similarity transformation: scaling, rotation and translation.
            - "QuickRigid": Rigid transformation: Only rotation and translation.
              May be useful for quick visualization fixes.
            - "DenseRigid": Rigid transformation: Only rotation and translation.
              Employs dense sampling during metric estimation.
            - "BOLDRigid": Rigid transformation: Parameters typical for BOLD to
              BOLD intrasubject registration.
            - "Affine": Affine transformation: Rigid + scaling.
            - "AffineFast": Fast version of Affine.
            - "BOLDAffine": Affine transformation: Parameters typical for BOLD to
              BOLD intrasubject registration.
            - "TRSAA": translation, rigid, similarity, affine (twice).

        template: set to true to register the data in template space
        inter_type_apply_transform_registration: choice of interpolator for apply affine transform of registration
            0 (Linear), 1 (NearestNeighbor), 2 (MultiLabel), 3 (Gaussian), 4 (BSpline), 4 (CosineWindowedSinc),
            6 (WelchWindowedSinc), 7 (HammingWindowedSinc), 8 (LanczosWindowedSinc), 9 (GenericLabel) default: 4.
        do_ss: use HD-BET to skull-strip the reference and apply mask on other modalities.
            do_coregistration need to be set to True to have a reference
        normalize_z_score: normalize skull-stripped image by substraction the mean of the whole brain
            (considers non zero) and dividing by the standard deviation
        scaling_factor_z_score: scaling factor to apply to normalization (default: 1)
        device: device to run HD-BT (default GPU: "0"), you can use "cpu".
        overwrite: if a step file already exist. will overwrite it. (default False)
        save_step: specify the step that you want to save : ("resample", "n4_correction", "coregistration",
            "affine_transform", "skullstripping", "normalize")

                - resample: save the resampling image'
                - n4_correction: save bias field correction with already applied previous step
                - coregistration: save coregistered image with already applied previous step
                - affine_transform: save affine transform of registration
                - mask: save brain mask resulting of ss of reference
                - ss: save all skull-stripped image

    .. document private functions
    .. automethod:: _run_normalize_z_score
    .. automethod:: _run_resample_image
    .. automethod:: _run_bias_field_correction
    .. automethod:: _run_coregistration
    .. automethod:: _run_skull_stripping
    """

    def __init__(self,
                 dict_image: Dict[str, str],
                 reference: Dict[str, str],
                 output_folder: str,
                 reorient_image: Optional[str] = None,
                 resample_spacing: Optional[Tuple[int, int, int]] = None,
                 inter_type_resample: int = 4,
                 n4_correction: Optional[List] = None,
                 do_coregistration: bool = True,
                 type_of_transform: str = "Affine",
                 template: bool = True,
                 inter_type_apply_transform_registration: int = 4,
                 do_ss: bool = True,
                 normalize_z_score: Optional[List] = None,
                 scaling_factor_z_score: int = 1,
                 device: str = "0",
                 overwrite: bool = False,
                 save_step: Sequence[str] = ("reorient",
                                             "resample",
                                             "n4_correction",
                                             "coregistration",
                                             "affine_transform",
                                             "skullstripping",
                                             "normalize"),
                 ) -> None:

        self.dict_image = dict_image
        self.reference = reference
        self.output_folder = output_folder
        self.reorient_image = reorient_image
        self.resample_spacing = resample_spacing
        self.inter_type_resample = inter_type_resample
        self.inter_type_apply_transform_registration = inter_type_apply_transform_registration
        self.n4_correction = n4_correction
        self.template = template
        self.do_coregistration = do_coregistration
        self.type_of_transform = type_of_transform
        self.do_ss = do_ss
        self.normalize_z_score = normalize_z_score
        self.scaling_factor_z_score = scaling_factor_z_score
        self.device = device
        self.overwrite = overwrite
        self.save_step = list(save_step)
        self.default_step = ("reorient",
                             "resample",
                             "n4_correction",
                             "coregistration",
                             "affine_transform",
                             "skullstripping",
                             "normalize")
        self.default_inter_type_apply_transform_registration = {0: "linear",
                                                                1: "nearestNeighbor",
                                                                2: "multiLabel",
                                                                3: "gaussian",
                                                                4: "bSpline",
                                                                5: "cosineWindowedSinc",
                                                                6: "welchWindowedSinc",
                                                                7: "hammingWindowedSinc",
                                                                8: "lanczosWindowedSinc",
                                                                9: "genericLabel"}

        self.default_type_of_transform = ["Translation", "Rigid", "Similarity", "QuickRigid", "DenseRigid", "BOLDRigid",
                                          "Affine", "AffineFast", "BOLDAffine", "TRSAA"]

        assert len(self.reference) == 1, "Reference must be unique and contains a key representing modality and " \
                                         "a value corresponding to the path"

        if self.reorient_image:
            assert self.reorient_image in ants.get_possible_orientations(), f"Possible orientation for reorientation" \
                                                                            f" are {ants.get_possible_orientations()}"

        if self.resample_spacing:
            assert len(self.resample_spacing) == 3, f"Resample spacing need to contains 3 values," \
                                                    f" actually {len(self.resample_spacing)}"

        if self.n4_correction:
            assert set(self.n4_correction).issubset(
                self.dict_image.keys()), f"n4_correction {self.n4_correction} must be in " \
                                         f"dict_image {self.dict_image.keys()}"
        if self.normalize_z_score:
            assert set(self.normalize_z_score).issubset(
                self.dict_image.keys()), f"normalize_z_score {self.normalize_z_score} must be in " \
                                         f"dict_image {self.dict_image.keys()}"

        assert self.inter_type_apply_transform_registration in self.default_inter_type_apply_transform_registration, \
            f"inter_type_apply_transform_registration must be comprise between 0 and 9: " \
            f"{self.default_inter_type_apply_transform_registration}"
        self.inter_type_apply_transform_registration = self.default_inter_type_apply_transform_registration[
            self.inter_type_apply_transform_registration]

        assert self.type_of_transform in self.default_type_of_transform, f"type_of_transform must be in:" \
                                                                         f" {self.default_type_of_transform}"

        if do_ss:
            if not do_coregistration:
                assert len(dict_image) == 1, "if not do_coregistration, you need to only pass one image to dict_image"

        for saving_step in self.save_step:
            assert saving_step in self.default_step, f"{saving_step} must be in {self.default_step}"

        if len(self.save_step) < 1:
            logger.warning("You don't save any step")

        # Check modalities files exist and check for adding a suffix
        for mod, mod_path in self.dict_image.items():
            assert os.path.exists(mod_path), f"{mod} path not exist"
            _, fnm, ext = split_filename(mod_path)

        assert os.path.exists(self.reference[list(self.reference.keys())[0]]), \
            f"{list(self.reference.values())[0]} path not exist"

        if self.template:
            self.template = os.path.join(os.path.dirname(os.path.abspath(utils.__file__)), "Atlas_SRI",
                                         "mni_icbm152_t1_tal_nlin_asym_09a.nii.gz")

        self.device = int(device) if torch.cuda.is_available() else "cpu"
        self.brain_mask = None

    def _get_steps_order(self) -> Dict[str, str]:
        """
        get steps order

        Returns: list of steps order
        """
        possibles_steps = OrderedDict({"reorient": self.reorient_image,
                                       "n4_correction": self.n4_correction,
                                       "resample": self.resample_spacing,
                                       "coregistration": self.do_coregistration,
                                       "skullstripping": self.do_ss,
                                       "normalize": self.normalize_z_score
                                       })

        correspondance_folder_file = {"reorient": "reorient",
                                      "n4_correction": "n4",
                                      "resample": "resample",
                                      "coregistration": "register",
                                      "skullstripping": "ss",
                                      "normalize": "normalize"}

        filtered_steps = OrderedDict(
            {k: cv for (k, v), (ck, cv) in zip(possibles_steps.items(), correspondance_folder_file.items()) if v})

        self.step_order = filtered_steps
        return filtered_steps

    def _get_step_dict_from_current_step(self, original_img_dict: Dict[str, str], current_step: str):
        """
        get step dict from current step

        :param original_img_dict: image dict with key is an identifier and value the corresponding image path
        :param current_step: current step of preprocessing
        :return: updated image dict
        """
        if current_step not in self.step_order:
            raise ValueError(f"current_step {current_step} is not recognize in {list(self.step_order.keys())}")
        name_step = list(self.step_order.values())[:list(self.step_order.keys()).index(current_step)]
        name_folder = list(self.step_order.keys())[
            list(self.step_order.keys()).index(current_step) - 1]
        if name_step:
            name_step = "_".join(name_step)
            step_dict = {}
            for mod, mod_path in original_img_dict.items():
                pth, fnm, ext = split_filename(mod_path)
                # n4 is specific to mod, so check
                if "n4" in name_step and mod not in self.n4_correction:
                    name_step = name_step.replace("n4", "")
                fnm_step = self.check_output_filename(fnm, mod, name_step)
                step_dict[mod] = os.path.join(self.output_folder, name_folder,
                                              fnm_step + ext)
            return step_dict
        else:
            return original_img_dict

    def _create_folders_steps(self) -> None:
        # create intermediate folder
        folders_steps = list(self._get_steps_order().keys())
        folders_steps.extend(["affine_transform"]) if (
                "affine_transform" in self.save_step and self.do_coregistration) else folders_steps
        for folder in folders_steps:
            if not os.path.exists(os.path.join(self.output_folder, folder)):
                os.makedirs(os.path.join(self.output_folder, folder), exist_ok=True)

    @staticmethod
    def check_output_filename(filename: str, modality: str, step: str) -> str:
        """
        check output filename. if mod is already in filename will not add it

        :param filename: filename
        :param modality: modality
        :param step: cBrainMRIPrePro step
        :return: filename
        """
        if step:
            if modality not in filename:
                filename += f"_{step}_{modality}"
            else:
                fnm = filename.split(modality)
                fnm.extend([step, modality])
                filename = re.sub("_+", "_", safe_file_name("_".join(fnm)))

        return filename

    @staticmethod
    def save_image(img: ants.ANTsImage, output_filename: str) -> None:
        """
        Save an `ants.ANTsImage` using :func:`ants.image_write`

        :param img: an ants.ANTsImage
        :param output_filename: output filename path
        """
        ants.image_write(image=img, filename=output_filename)

    def _save_transform(self, img: ants.ANTsTransform, filename: str, modality: str, step: str, save_folder: str,
                        ext: str) -> None:
        filename = self.check_output_filename(filename=filename, modality=modality, step=step)
        output_filename = os.path.join(self.output_folder, save_folder, f"{filename}{ext}")
        ants.write_transform(transform=img, filename=output_filename)

    def _run_reorient_image(self, img_dict: Dict[str, str]) -> None:
        """
        Run image reorientation using :func:`ants.reorient_image2`

        :param img_dict: image dict with key is an identifier and value the corresponding image path
        """
        logger.info("Perform reorientation")
        for mod, mod_path in img_dict.items():
            _, fnm, ext = split_filename(mod_path)
            output_filename = os.path.join(self.output_folder, "reorient",
                                           self.check_output_filename(filename=fnm,
                                                                      modality=mod,
                                                                      step="reorient") + ext)
            if os.path.exists(output_filename) and not self.overwrite:
                logger.warning(f"Already exist and not overwrite, So pass ... {output_filename}")
                continue
            logger.info(f"Process: {output_filename}")
            img_reorient = ants.reorient_image2(image=ants.image_read(mod_path), orientation=self.reorient_image)
            ants.image_write(image=img_reorient, filename=output_filename)

    def _run_resample_image(self, img_dict: Dict[str, str]) -> None:
        """
        Run image resampling using :func:`ants.resample_image`

        :param img_dict: image dict with key is an identifier and value the corresponding image path
        """
        logger.info("Perform resampling")
        for mod, mod_path in img_dict.items():
            _, fnm, ext = split_filename(mod_path)
            output_filename = os.path.join(self.output_folder, "resample",
                                           self.check_output_filename(filename=fnm,
                                                                      modality=mod,
                                                                      step="resample") + ext)
            if os.path.exists(output_filename) and not self.overwrite:
                logger.warning(f"Already exist and not overwrite, So pass ... {output_filename}")
                continue
            logger.info(f"Process: {output_filename}")
            img_resample = ants.resample_image(image=ants.image_read(mod_path),
                                               resample_params=self.resample_spacing,
                                               interp_type=self.inter_type_resample)
            ants.image_write(image=img_resample, filename=output_filename)

    def _run_bias_field_correction(self, img_dict: Dict[str, str]) -> None:
        """
        Run n4 bias field correction using :func:`ants.n4_bias_field_correction`

        .. warning::
            N4 includes a log transform, so we are be aware of negative values. The function will check range of image
            intensities and rescale to positive if negative values.

            0) get range of input image = [a,b]
            1) rescale input image to be in a positive, e.g., [10 , 1000]
            2) perform N4 on 1)
            3) rescale image from 2) to be in [a,b]

            See Also: `<https://github.com/ANTsX/ANTs/issues/822>`_

        .. note::
            To reproduce behavior as in ANTs a mask equally weighting the entire image is supplied.
            In ANTsPy when no mask is supplied, a mask is computed with the get_mask function. Function is based
            on a mean threshold. Resulting head mask is very often filled with holes.
            Here we compute a real head mask with no holes with the :py:func:`.utils.image_processing.get_mask`

        :param img_dict: image dict with key is an identifier and value the corresponding image path
        """
        logger.info("Perform bias field correction")
        for mod_n4 in self.n4_correction:
            _, fnm, ext = split_filename(img_dict[mod_n4])
            output_filename = os.path.join(self.output_folder, "n4_correction",
                                           self.check_output_filename(filename=fnm,
                                                                      modality=mod_n4,
                                                                      step="n4") + ext)

            if os.path.exists(output_filename) and not self.overwrite:
                logger.warning(f"Already exist and not overwrite, So pass ... {output_filename}")
                continue
            logger.info(f"Process: {output_filename}")
            img = ants.image_read(img_dict[mod_n4])
            img_array = img.numpy()
            img_array_scaled, min_, scale_ = min_max_scaling(input_array=img_array, scaling_range=(10, 1000))
            img = img.new_image_like(img_array_scaled)
            # get head mask
            head_mask = get_mask(img_array)
            head_mask = img.new_image_like(head_mask).astype("float32")  # pass to float32 to be read by n4 function
            img_n4 = ants.n4_bias_field_correction(image=img, mask=head_mask, verbose=False)  # 2
            img_array_n4 = img_n4.numpy()
            img_array_unscaled = invert_min_max_scaling(input_array_scaled=img_array_n4, scale_=scale_, min_=min_)
            img_n4 = img.new_image_like(img_array_unscaled)  # 3
            ants.image_write(image=img_n4, filename=output_filename)

    def _run_coregistration(self, img_dict: Dict[str, str], reference: Dict[str, str]) -> None:
        """
        Run coregistration using :func:`ants.registration`.

        :param img_dict: image dict with key is an identifier and value the corresponding image path
        :param reference: reference dict with key is an identifier and value the corresponding image path.
            Need to be a dict of length 1.
        """
        logger.info("Perform coregistration")
        save_affine_transform = ["fwdtransforms", "invtransforms"]
        _, fnm_ref, ext_ref = split_filename(list(reference.values())[0])

        step_rf = "register" if list(reference.values())[0] in img_dict else "reference"
        output_filename_ref = os.path.join(self.output_folder, "coregistration",
                                           self.check_output_filename(filename=fnm_ref,
                                                                      modality=list(reference.keys())[0],
                                                                      step=step_rf) + ext_ref)
        check_affine_transform_ref_save = []
        if "affine_transform" in self.save_step:
            for transform in save_affine_transform:
                check_affine_transform_ref_save.append(
                    os.path.exists(os.path.join(self.output_folder, "affine_transform",
                                                self.check_output_filename(filename=fnm_ref,
                                                                           modality=list(reference.keys())[0],
                                                                           step=transform) + ".mat")))

        if not self.overwrite and output_filename_ref and all(check_affine_transform_ref_save):
            logger.warning(f"Already exist and not overwrite, So pass ... {output_filename_ref}")
            pass
        else:
            if self.template:
                logger.info(f"Process: {output_filename_ref}")
                fixed_image = ants.image_read(self.template)
                moving_image = ants.image_read(list(reference.values())[0])
                reg = ants.registration(fixed=fixed_image, moving=moving_image, type_of_transform="Affine")
                warped_image = ants.apply_transforms(fixed=fixed_image, moving=moving_image,
                                                     transformlist=reg['fwdtransforms'],
                                                     interpolator=self.inter_type_apply_transform_registration)
                ants.image_write(image=warped_image, filename=output_filename_ref)

                if "affine_transform" in self.save_step:
                    for transform in reg:
                        if transform in save_affine_transform:
                            self._save_transform(img=ants.read_transform(reg[transform][0]), filename=fnm_ref,
                                                 modality=list(reference.keys())[0],
                                                 step=transform, save_folder="affine_transform", ext=".mat")
            else:
                shutil.copy(src=list(reference.values())[0],
                            dst=os.path.join(self.output_folder, "coregistration",
                                             self.check_output_filename(filename=fnm_ref,
                                                                        modality=list(reference.keys())[0],
                                                                        step="reference") + ext_ref))

        modalities_to_register = copy.deepcopy(img_dict)
        if list(reference.values())[0] in img_dict:
            modalities_to_register.pop(list(reference.keys())[0], None)
        for mod, mod_path in modalities_to_register.items():
            _, fnm, ext = split_filename(mod_path)
            output_filename = os.path.join(self.output_folder, "coregistration",
                                           self.check_output_filename(filename=fnm,
                                                                      modality=mod,
                                                                      step="register") + ext)

            check_affine_transform_save = []
            if "affine_transform" in self.save_step:
                for transform in save_affine_transform:
                    check_affine_transform_save.append(
                        os.path.exists(os.path.join(self.output_folder, "affine_transform",
                                                    self.check_output_filename(
                                                        filename=fnm,
                                                        modality=mod,
                                                        step=transform) + ".mat")))

            if os.path.exists(output_filename) and all(check_affine_transform_save) and not self.overwrite:
                logger.warning(f"Already exist and not overwrite, So pass ... {output_filename}")
                continue
            logger.info(f"Process: {output_filename}")
            fixed_image = ants.image_read(output_filename_ref)
            moving_image = ants.image_read(mod_path)
            reg = ants.registration(fixed=fixed_image, moving=moving_image, type_of_transform='Affine')
            warped_image = ants.apply_transforms(fixed=fixed_image, moving=moving_image,
                                                 transformlist=reg['fwdtransforms'], interpolator="bSpline")
            ants.image_write(image=warped_image, filename=output_filename)

            if "affine_transform" in self.save_step:
                for transform in reg:
                    if transform in save_affine_transform:
                        self._save_transform(img=ants.read_transform(reg[transform][0]), filename=fnm,
                                             modality=mod, step=transform, save_folder="affine_transform",
                                             ext=".mat")

    def _run_skull_stripping(self, img_dict: Dict[str, str], reference: Dict[str, str]) -> None:
        """
        Run skull stripping using :func:`run_hd_bet` from :class:`HD_BET`.

        :param img_dict: image dict with key is an identifier and value the corresponding image path
        :param reference: reference dict with key is an identifier and value the corresponding image path.
            Need to be a dict of length 1.
        """
        logger.info("Perform Skull Stripping using HD-BET")
        ref_path = list(reference.values())[0]
        _, fnm_ref, ext_ref = split_filename(ref_path)
        output_fname_ss = os.path.join(self.output_folder, "skullstripping", fnm_ref + "_ss" + ext_ref)
        # get correct mask name
        filename_mask = self.check_output_filename(filename=fnm_ref,
                                                   modality=list(reference.keys())[0],
                                                   step="brain_mask") + ext_ref
        new_filename_mask = os.path.join(self.output_folder, "skullstripping", filename_mask)
        self.brain_mask = new_filename_mask

        if os.path.exists(new_filename_mask) and not self.overwrite:
            logger.warning(f"Already exist and not overwrite, So pass ... {new_filename_mask}")
            pass
        else:
            logger.info(f"Create brain mask")
            run_hd_bet(mri_fnames=list(reference.values())[0], output_fnames=output_fname_ss,
                       device=self.device)
            os.remove(output_fname_ss)

            os.rename(src=os.path.join(self.output_folder, "skullstripping", fnm_ref + "_ss" + "_mask" + ext_ref),
                      dst=new_filename_mask)

        # Fill if still hole in hd-bet mask (sometimes ventricles are not included)
        mask_array, mask_header = load_nifty_volume_as_array(input_path_file=new_filename_mask)
        mask_array = ndimage.binary_fill_holes(mask_array)
        save_to_nii(im=mask_array, header=mask_header, output_dir=os.path.dirname(new_filename_mask),
                    filename=os.path.basename(new_filename_mask), mode="label")

        for mod, mod_path in img_dict.items():  # loose some seconds because skull-strip reference again
            _, fnm, ext = split_filename(mod_path)
            filename_mod = self.check_output_filename(filename=fnm, modality=mod, step="ss") + ext_ref
            output_filename_mod = os.path.join(self.output_folder, "skullstripping",
                                               filename_mod + ".nii.gz")
            if os.path.exists(output_filename_mod) and not self.overwrite:
                logger.warning(f"Already exist and not overwrite, So pass ... {output_filename_mod}")

                continue
            logger.info(f"Process: {output_filename_mod}")
            file_array, file_header = self.apply_brain_mask(input_file_path=mod_path, mask=mask_array)
            save_to_nii(im=file_array, header=file_header,
                        output_dir=os.path.join(self.output_folder, "skullstripping"),
                        filename=filename_mod, mode="image", gzip=True)

    def _run_normalize_z_score(self, img_dict: Dict[str, str]) -> None:
        """
        Run z-score normalization in brain.

        .. math::
            I_{z-score}(\mathbf x) = \dfrac{I(\mathbf x) - \mu}{\sigma}.

        :param img_dict: image dict with key is an identifier and value the corresponding image path
        """
        logger.info("Perform z-score normalization")
        for mod_normalize in self.normalize_z_score:
            _, fnm, ext = split_filename(img_dict[mod_normalize])
            output_filename = os.path.join(self.output_folder, "normalize",
                                           self.check_output_filename(filename=fnm,
                                                                      modality=mod_normalize,
                                                                      step="normalize") + ext)
            if os.path.exists(output_filename) and not self.overwrite:
                logger.warning(f"Already exist and not overwrite, So pass ... {output_filename}")
                continue
            logger.info(f"Process: {output_filename}")
            img = ants.image_read(img_dict[mod_normalize])
            img_array = img.numpy()
            img_array_normalize = zscore_normalize(input_array=img_array,
                                                   scaling_factor=self.scaling_factor_z_score,
                                                   mask=None)  # don't use self.brain_mask because if image has a crop
            # will take a lot of background in normalization
            img = img.new_image_like(img_array_normalize)
            ants.image_write(image=img, filename=output_filename)

    @staticmethod
    def apply_brain_mask(input_file_path: str, mask: Union[str, np.ndarray]) \
            -> Tuple[np.ndarray, Tuple[Tuple, Tuple, Tuple]]:
        """
        Apply brain mask on head image

        :param input_file_path: input file path
        :param mask: input brain mask file path or array-like
        :return: array, header
        """
        mask_array = mask
        if isinstance(mask, str):
            mask_array, _ = load_nifty_volume_as_array(input_path_file=mask)

        file_array, file_header = load_nifty_volume_as_array(input_path_file=input_file_path)

        file_array[mask_array == 0] = 0

        return file_array, file_header

    def run_pipeline(self):
        """
        Main function to run :class:`DataPreprocessing`
        """
        self._create_folders_steps()

        step_dict, reference_dict, step, folder_path = {}, {}, "", {}

        if self.reorient_image:
            self._run_reorient_image(
                img_dict=self._get_step_dict_from_current_step(self.dict_image, current_step="reorient"))

        if self.n4_correction:
            self._run_bias_field_correction(
                img_dict=self._get_step_dict_from_current_step(self.dict_image, current_step="n4_correction"))

        if self.resample_spacing:
            self._run_resample_image(
                img_dict=self._get_step_dict_from_current_step(self.dict_image, current_step="resample"))

        if self.do_coregistration:
            step_dict = self._get_step_dict_from_current_step(self.dict_image, current_step="coregistration")
            reference_dict = {list(self.reference.keys())[0]: step_dict[list(self.reference.keys())[0]]} if \
                list(self.reference.values())[0] in self.dict_image.values() else copy.deepcopy(self.reference)
            self._run_coregistration(img_dict=step_dict, reference=reference_dict)

        if self.do_ss:
            step_dict = self._get_step_dict_from_current_step(self.dict_image, current_step="skullstripping")

            if self.do_coregistration:
                reference_dict = {
                    k: os.path.join(self.output_folder, "coregistration",
                                    self.check_output_filename(filename=split_filename(v)[1], modality=k,
                                                               step="register") +
                                    split_filename(v)[2]) for k, v in reference_dict.items()}
            else:
                reference_dict = step_dict

            self._run_skull_stripping(img_dict=step_dict, reference=reference_dict)

        if self.normalize_z_score:
            self._run_normalize_z_score(
                img_dict=self._get_step_dict_from_current_step(self.dict_image, current_step="normalize"))

        # if step to remove
        remove_step = set(self.save_step).symmetric_difference(set(self.default_step))
        if remove_step:
            for step_to_remove in remove_step:
                shutil.rmtree(path=os.path.join(self.output_folder, step_to_remove), ignore_errors=True)
