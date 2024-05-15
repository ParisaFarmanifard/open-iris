from typing import Collection, Tuple

import numpy as np
from pydantic import PositiveInt

from iris.io.class_configs import Algorithm
from iris.io.dataclasses import EyeOrientation, GeometryPolygons, IRImage, NoiseMask, NormalizedIris
from iris.io.errors import NormalizationError
from iris.nodes.normalization.common import (
    correct_orientation,
    generate_iris_mask,
    getgrids,
    to_uint8,
)
from iris.utils import math


class NonlinearNormalization(Algorithm):
    """Implementation of a normalization algorithm which uses nonlinear squared transformation to map image pixels.
    
    Algorithm steps:
        1) Create nonlinear grids of sampling radii based on parameters: res_in_r, intermediate_radiuses.
        2) Compute the mapping between the normalized image pixel location and the original image location.
        3) Obtain pixel values of normalized image using bilinear intepolation.
    """

    class Parameters(Algorithm.Parameters):
        """Parameters class for NonlinearNormalization."""

        res_in_r: PositiveInt
        intermediate_radiuses: Collection[float]
        oversat_threshold: PositiveInt

    __parameters_type__ = Parameters

    def __init__(
        self,
        res_in_r: PositiveInt = 128,
        oversat_threshold: PositiveInt = 254,
    ) -> None:
        """Assign parameters.

        Args:
            res_in_r (PositiveInt): Normalized image r resolution. Defaults to 128.
            oversat_threshold (PositiveInt, optional): threshold for masking over-satuated pixels. Defaults to 254.
        """
        intermediate_radiuses = np.array(
            [getgrids(max(0, res_in_r), p2i_ratio) for p2i_ratio in range(100)]
        )
        super().__init__(
            res_in_r=res_in_r,
            intermediate_radiuses=intermediate_radiuses,
            oversat_threshold=oversat_threshold,
        )

    def run(
        self,
        image: IRImage,
        noise_mask: NoiseMask,
        extrapolated_contours: GeometryPolygons,
        eye_orientation: EyeOrientation,
    ) -> NormalizedIris:
        """Normalize iris using nonlinear transformation when sampling points from cartisian to polar coordinates.

        Args:
            image (IRImage): Input image to normalize.
            noise_mask (NoiseMask): Noise mask.
            extrapolated_contours (GeometryPolygons): Extrapolated contours.
            eye_orientation (EyeOrientation): Eye orientation angle.

        Returns:
            NormalizedIris: NormalizedIris object containing normalized image and iris mask.
        """
        if len(extrapolated_contours.pupil_array) != len(extrapolated_contours.iris_array):
            raise NormalizationError("The number of extrapolated iris and pupil points must be the same.")

        pupil_points, iris_points = correct_orientation(
            extrapolated_contours.pupil_array,
            extrapolated_contours.iris_array,
            eye_orientation.angle,
        )

        iris_mask = generate_iris_mask(extrapolated_contours, noise_mask.mask)
        iris_mask[image.img_data >= self.params.oversat_threshold] = False
        src_points = self._generate_correspondences(pupil_points, iris_points)

        normalized_image, normalized_mask = self._normalize_all(
            original_image=image.img_data, iris_mask=iris_mask, src_points=src_points
        )
        normalized_iris = NormalizedIris(
            normalized_image=to_uint8(normalized_image),
            normalized_mask=normalized_mask,
        )
        return normalized_iris

    def _generate_correspondences(self, pupil_points: np.ndarray, iris_points: np.ndarray) -> np.ndarray:
        """Generate corresponding positions in original image.

        Args:
            pupil_points (np.ndarray): Pupil bounding points. NumPy array of shape (num_points x 2).
            iris_points (np.ndarray): Iris bounding points. NumPy array of shape (num_points x 2).

        Returns:
            np.ndarray: generated corresponding points.
        """
        pupil_diameter = math.estimate_diameter(pupil_points)
        iris_diameter = math.estimate_diameter(iris_points)
        p2i_ratio = pupil_diameter / iris_diameter

        if p2i_ratio <= 0 or p2i_ratio >= 1:
            raise NormalizationError(f"Invalid pupil to iris ratio, not in the range (0,1): {p2i_ratio}.")

        src_points = np.array(
            [
                pupil_points + x * (iris_points - pupil_points)
                for x in self.params.intermediate_radiuses[round(100 * (p2i_ratio))]
            ]
        )

        return np.round(src_points).astype(int)
    
    def _normalize_all(
        self,
        original_image: np.ndarray,
        iris_mask: np.ndarray,
        src_points: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Normalize all points of an image using bilinear.

        Args:
            original_image (np.ndarray): Entire input image to normalize.
            iris_mask (np.ndarray): Iris class segmentation mask.
            src_points (np.ndarray): original input image points.

        Returns:
            t.Tuple[np.ndarray, np.ndarray]: Tuple with normalized image and mask.
        """
        src_shape = src_points.shape[0:2]
        src_points = np.vstack(src_points)
        image_size = original_image.shape
        src_points[src_points[:, 0] >= image_size[1], 0] = -1
        src_points[src_points[:, 1] >= image_size[0], 1] = -1

        normalized_image = np.array(
            [original_image[image_xy[1], image_xy[0]] if min(image_xy) >= 0 else 0 for image_xy in src_points]
        )
        normalized_image = np.reshape(normalized_image, src_shape)

        normalized_mask = np.array(
            [iris_mask[image_xy[1], image_xy[0]] if min(image_xy) >= 0 else False for image_xy in src_points]
        )
        normalized_mask = np.reshape(normalized_mask, src_shape)

        return normalized_image / 255.0, normalized_mask

    