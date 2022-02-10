from abc import abstractmethod
from copy import copy, deepcopy
from typing import Union, Sequence, Tuple, Dict, List

import dask
import dask.array as da
import numpy as np
from ase import Atoms
from numba.core.errors import NumbaPerformanceWarning

from abtem.core.antialias import AntialiasAperture
from abtem.core.axes import FrozenPhononsAxis, OrdinalAxis, RealSpaceAxis, AxisMetadata, PrismPlaneWavesAxis
from abtem.core.backend import get_array_module, cp, copy_to_device, _validate_device
from abtem.core.complex import complex_exponential
from abtem.core.dask import HasDaskArray, validate_lazy, ComputableList
from abtem.core.energy import Accelerator
from abtem.core.grid import Grid
from abtem.core.utils import generate_chunks
from abtem.ionization.multislice import linear_scaling_transition_multislice
from abtem.measure.detect import AbstractDetector, validate_detectors, stack_measurement_ensembles, \
    allocate_measurements
from abtem.measure.measure import AbstractMeasurement
from abtem.potentials.potentials import AbstractPotential, validate_potential
from abtem.waves.base import WavesLikeMixin
from abtem.waves.multislice import multislice
from abtem.waves.prism_utils import prism_wave_vectors, partitioned_prism_wave_vectors, plane_waves, remove_tilt, \
    interpolate_full, beamlet_basis, reduce_beamlets_nearest_no_interpolation, wrapped_crop_2d
from abtem.waves.scan import AbstractScan, GridScan, LineScan, CustomScan, validate_scan
from abtem.waves.tilt import BeamTilt
from abtem.waves.transfer import CTF
from abtem.waves.waves import Waves, Probe, MetaWaves


def batch_crop_2d(array: np.ndarray, corners: Tuple[int, int], new_shape: Tuple[int, int]):
    xp = get_array_module(array)
    if xp is cp:
        i = xp.arange(array.shape[0])[:, None, None]
        ix = cp.arange(new_shape[0]) + cp.asarray(corners[:, 0, None])
        iy = cp.arange(new_shape[1]) + cp.asarray(corners[:, 1, None])
        ix = ix[:, :, None]
        iy = iy[:, None]
        return array[i, ix, iy]
    else:
        array = np.lib.stride_tricks.sliding_window_view(array, (1,) + new_shape)
        return array[xp.arange(array.shape[0]), corners[:, 0], corners[:, 1], 0]


_plane_waves_axes_metadata = {'label': 'plane_waves', 'type': 'ensemble'}


def prism_coefficients(positions: np.ndarray, wave_vectors: np.ndarray, wavelength: float, ctf: CTF):
    xp = get_array_module(wave_vectors)
    positions = copy_to_device(positions, xp)

    def calculate_ctf_coefficient(wave_vectors, wavelength, ctf):
        alpha = xp.sqrt(wave_vectors[:, 0] ** 2 + wave_vectors[:, 1] ** 2) * wavelength
        phi = xp.arctan2(wave_vectors[:, 0], wave_vectors[:, 1])
        coefficients = ctf.evaluate(alpha, phi)
        return coefficients

    def calculate_translation_coefficients(wave_vectors, positions):
        coefficients = complex_exponential(-2. * xp.pi * positions[..., 0, None] * wave_vectors[:, 0][None])
        coefficients *= complex_exponential(-2. * xp.pi * positions[..., 1, None] * wave_vectors[:, 1][None])
        return coefficients

    return calculate_ctf_coefficient(wave_vectors, wavelength=wavelength, ctf=ctf) * \
           calculate_translation_coefficients(wave_vectors, positions)


def _minimum_crop(positions: Union[Sequence[float], AbstractScan], sampling, shape):
    if isinstance(positions, AbstractScan):
        positions = np.array(positions.limits)

    xp = get_array_module(positions)

    offset = (shape[0] // 2, shape[1] // 2)
    corners = xp.rint(xp.array(positions) / xp.asarray(sampling) - xp.asarray(offset)).astype(int)
    upper_corners = corners + xp.asarray(shape)

    crop_corner = (xp.min(corners[..., 0]).item(), xp.min(corners[..., 1]).item())

    size = (xp.max(upper_corners[..., 0]).item() - crop_corner[0],
            xp.max(upper_corners[..., 1]).item() - crop_corner[1])

    corners -= xp.asarray(crop_corner)
    return crop_corner, size, corners


def validate_interpolation(interpolation):
    if isinstance(interpolation, int):
        interpolation = (interpolation,) * 2
    elif not len(interpolation) == 2:
        raise ValueError('interpolation factor must be int')
    return interpolation


def recursive_generate_items(l, ):
    for el in l:
        if isinstance(el, list) and not isinstance(el, (str, bytes)):
            yield from recursive_generate_items(el)
        else:
            yield el


def _split_list(lst, division):
    return [lst[i:i + division] for i in range(0, len(lst), division)]


def _concatenate_blocks(list_of_arrays, block_shape):
    if len(block_shape) == 1:
        return np.concatenate(list_of_arrays, axis=0)
    elif len(block_shape) == 2:
        assert len(list_of_arrays) == block_shape[0] * block_shape[1]
        measurement = _split_list(list_of_arrays, block_shape[1])
        return np.concatenate([np.concatenate(block, axis=1) for block in measurement], axis=0)


def list_shape(lst, shape=()):
    if not isinstance(lst, list):
        return shape

    if isinstance(lst[0], list):
        if not all(len(item) == len(lst[0]) for item in lst):
            msg = 'not all lists have the same length'
            raise ValueError(msg)

    shape += (len(lst),)
    shape = list_shape(lst[0], shape)
    return shape


def _reduce_partitioned(s_matrix, basis, positions: np.ndarray, axes_metadata) -> Waves:
    if len(axes_metadata) != (len(positions.shape) - 1):
        raise RuntimeError()

    shifts = np.round(positions.reshape((-1, 2)) / s_matrix.sampling).astype(int)
    shifts -= np.array(s_matrix.crop_offset)
    shifts -= (np.array(s_matrix.interpolated_gpts)) // 2

    # basis = np.moveaxis(basis, 0, 2).copy()
    # array = np.moveaxis(s_matrix.array, 0, 2).copy()

    import warnings
    warnings.simplefilter('ignore', category=NumbaPerformanceWarning)

    waves = np.zeros((len(shifts),) + s_matrix.interpolated_gpts, dtype=np.complex64)
    reduce_beamlets_nearest_no_interpolation(waves, basis, s_matrix.array, shifts)
    waves = waves.reshape(positions.shape[:-1] + waves.shape[-2:])

    waves = Waves(waves,
                  sampling=s_matrix.sampling,
                  energy=s_matrix.energy,
                  extra_axes_metadata=axes_metadata,
                  antialias_cutoff_gpts=s_matrix.antialias_cutoff_gpts)

    return waves


def _reduce(s_matrix, basis, positions: np.ndarray, axes_metadata):
    xp = get_array_module(s_matrix._device)
    positions = xp.asarray(positions)

    if len(axes_metadata) != (len(positions.shape) - 1):
        raise RuntimeError()

    out_shape = positions.shape[:-1]
    positions = positions.reshape((-1, 2))
    offset_positions = positions - xp.array(s_matrix.crop_offset) * xp.array(s_matrix.sampling)

    wave_vectors = xp.asarray(s_matrix.wave_vectors)

    coefficients = complex_exponential(-2. * xp.pi * positions[..., 0, None] * wave_vectors[:, 0][None])
    coefficients *= complex_exponential(-2. * xp.pi * positions[..., 1, None] * wave_vectors[:, 1][None])
    coefficients *= basis

    if not s_matrix.array.shape[-2:] == s_matrix.interpolated_gpts:
        crop_corner, size, corners = _minimum_crop(offset_positions, s_matrix.sampling, s_matrix.interpolated_gpts)

        array = wrapped_crop_2d(s_matrix.array, crop_corner, size)

        if s_matrix._device == 'gpu':
            array = xp.asarray(array)

        array = xp.tensordot(coefficients, array, axes=[-1, -3])

        array = batch_crop_2d(array, corners.reshape((-1, 2)), s_matrix.interpolated_gpts)
    else:
        array = xp.tensordot(coefficients, s_matrix.array, axes=[-1, -3])

    array = array.reshape(out_shape + array.shape[-2:])

    antialias_cutoff_gpts = s_matrix.meta_waves.antialias_cutoff_gpts

    waves = Waves(array,
                  sampling=s_matrix.sampling,
                  energy=s_matrix.energy,
                  extra_axes_metadata=axes_metadata,
                  antialias_cutoff_gpts=antialias_cutoff_gpts,
                  metadata=s_matrix.metadata)

    return waves


# def reduce_s_matrix(s_matrix, detectors, scan, ctf, positions_per_reduction):
#     xp = get_array_module(s_matrix._device)
#
#     if s_matrix.partitions:
#         extent = (s_matrix.interpolated_gpts[0] * s_matrix.sampling[0],
#                   s_matrix.interpolated_gpts[1] * s_matrix.sampling[1])
#
#         wave_vectors = prism_wave_vectors(s_matrix.planewave_cutoff, extent, s_matrix.energy, (1, 1))
#
#         ctf = ctf.copy()
#         ctf.defocus = -s_matrix.accumulated_defocus
#
#         basis = beamlet_basis(ctf,
#                               s_matrix.wave_vectors,
#                               wave_vectors,
#                               s_matrix.interpolated_gpts,
#                               s_matrix.sampling).astype(np.complex64)
#
#     else:
#         wave_vectors = xp.asarray(s_matrix.wave_vectors)
#         alpha = xp.sqrt(wave_vectors[:, 0] ** 2 + wave_vectors[:, 1] ** 2) * s_matrix.wavelength
#         phi = xp.arctan2(wave_vectors[:, 0], wave_vectors[:, 1])
#         basis = ctf.evaluate(alpha, phi)
#
#     measurements = []
#     for indexing, positions in scan.generate_positions(chunks=positions_per_reduction):
#
#         if s_matrix.partitions is None:
#             waves = _reduce(s_matrix, basis, positions, scan.axes_metadata)
#         else:
#             waves = _reduce_partitioned(s_matrix, basis, positions, scan.axes_metadata)
#
#         for i, detector in enumerate(detectors):
#             try:
#                 measurements[i].array[indexing] = detector.detect(waves).array
#             except IndexError:
#                 single_wave = waves[(len(positions.shape) - 1) * (0,)]
#                 measurements.append(detector.allocate_measurement(single_wave, scan))
#                 measurements[i].array[indexing] = detector.detect(waves).array
#
#     return measurements

def reduce_s_matrix(s_matrix, detectors, scan, ctf, positions_per_reduction):
    measurements = None
    for i, (indices, waves) in enumerate(s_matrix._generate_waves(scan, ctf, positions_per_reduction)):

        if i == 0:
            meta_waves = s_matrix.meta_waves
            measurements = [detector.allocate_measurement(meta_waves, scan) for detector in detectors]

        for j, detector in enumerate(detectors):
            measurements[j].array[indices] = detector.detect(waves).array
    return measurements


def stack_s_matrices(s_matrices, axes_metadata):
    arrays = [s_matrix.array for s_matrix in s_matrices]
    d = s_matrices[0]._copy_as_dict(copy_array=False)

    if s_matrices[0].is_lazy:
        d['array'] = da.stack(arrays)
    else:
        xp = get_array_module(arrays[0])
        d['array'] = xp.stack(arrays)
    d['extra_axes_metadata'] = [axes_metadata]

    return SMatrixArray(**d)


class AbstractSMatrix(WavesLikeMixin):
    planewave_cutoff: float
    interpolation: Tuple[int, int]

    @property
    @abstractmethod
    def interpolated_gpts(self):
        pass

    # def equivalent_probe(self):
    #     gpts = self.interpolated_gpts
    #     extent = (gpts[0] * self.sampling[0], gpts[1] * self.sampling[1])
    #     probe = Probe(gpts=gpts, extent=extent, energy=self.energy, device=self.device)
    #     if self._antialias_cutoff_gpts is not None:
    #         probe._antialias_cutoff_gpts = (self._antialias_cutoff_gpts[0] // self.interpolation[0],
    #                                         self._antialias_cutoff_gpts[1] // self.interpolation[1])
    #     return probe

    @property
    def meta_waves(self):
        gpts = self.interpolated_gpts
        extent = (gpts[0] * self.sampling[0], gpts[1] * self.sampling[1])

        if self._antialias_cutoff_gpts is not None:
            antialias_cutoff_gpts = (self._antialias_cutoff_gpts[0] // self.interpolation[0],
                                     self._antialias_cutoff_gpts[1] // self.interpolation[1])
        else:
            antialias_cutoff_gpts = None
        waves = MetaWaves(gpts=gpts,
                          extent=extent,
                          energy=self.energy,
                          antialias_cutoff_gpts=antialias_cutoff_gpts,
                          extra_axes_shape=self.extra_axes_shape,
                          extra_axes_metadata=self.extra_axes_metadata,
                          device=self.device)
        return waves

    @property
    def base_axes_metadata(self) -> List[AxisMetadata]:
        self.grid.check_is_defined()
        return [OrdinalAxis(),
                RealSpaceAxis(label='x', sampling=self.sampling[0], units='Å', endpoint=False),
                RealSpaceAxis(label='y', sampling=self.sampling[0], units='Å', endpoint=False)]

    def _validate_ctf(self, ctf):
        if ctf is None:
            ctf = CTF(semiangle_cutoff=self.planewave_cutoff, energy=self.energy)

        if isinstance(ctf, dict):
            ctf = CTF(energy=self.energy, **ctf)

        return ctf

    def _validate_positions(self, positions, ctf):
        if positions is None:
            positions = (0., 0.)

        if isinstance(positions, GridScan):
            if positions.start is None:
                positions.start = (0., 0.)
            if positions.end is None:
                positions.end = self.extent
            if positions.sampling is None and ctf is not None:
                positions.sampling = 0.9 * ctf.nyquist_sampling
            return positions

        elif isinstance(positions, LineScan):
            raise NotImplementedError()

        elif isinstance(positions, CustomScan):
            return positions

        elif isinstance(positions, (list, tuple, np.ndarray)):
            return CustomScan(positions)

        elif not isinstance(positions, CustomScan):
            raise NotImplementedError


class SMatrixArray(HasDaskArray, AbstractSMatrix):
    """
    Scattering matrix array object.

    The scattering matrix array object represents a plane wave expansion of a probe, it is used for STEM simulations
    with the PRISM algorithm.

    Parameters
    ----------
    array : 3d array or 4d array
        The array representation of the scattering matrix.
    energy : float
        Electron energy [eV].
    wave_vectors : 2d array
        The spatial frequencies of each plane in the plane wave expansion.
    planewave_cutoff : float
        The angular cutoff of the plane wave expansion [mrad].
    interpolation : int or two int
        Interpolation factor. Default is 1 (no interpolation).
    sampling : one or two float, optional
        Lateral sampling of wave functions [1 / Å]. Default is None (inherits sampling from the potential).
    tilt : two float, optional
        Small angle beam tilt [mrad].
    antialias_aperture : two float, optional
        Assumed antialiasing aperture as a fraction of the real space Nyquist frequency. Default is 2/3.
    device : str, optional
        The calculations will be carried out on this device. Default is 'cpu'.
    extra_axes_metadata : list of dicts
    metadata : dict
    """

    def __init__(self,
                 array: Union[np.ndarray, da.core.Array],
                 energy: float,
                 wave_vectors: np.ndarray,
                 planewave_cutoff: float,
                 interpolation: Union[int, Tuple[int, int]] = 1,
                 partitions: int = None,
                 sampling: Union[float, Tuple[float, float]] = None,
                 tilt: Tuple[float, float] = None,
                 accumulated_defocus: float = 0.,
                 crop_offset: Tuple[int, int] = (0, 0),
                 uncropped_gpts: Tuple[int, int] = None,
                 antialias_cutoff_gpts: Tuple[int, int] = None,
                 normalization: str = 'probe',
                 device: str = None,
                 extra_axes_metadata: List[Dict] = None,
                 metadata: Dict = None):

        self._interpolation = validate_interpolation(interpolation)

        self._grid = Grid(gpts=array.shape[-2:], sampling=sampling, lock_gpts=True)

        self._beam_tilt = BeamTilt(tilt=tilt)
        self._antialias_cutoff_gpts = antialias_cutoff_gpts
        self._accelerator = Accelerator(energy=energy)
        self._device = _validate_device(device)

        self._array = array
        self._wave_vectors = wave_vectors
        self._planewave_cutoff = planewave_cutoff

        super().__init__(array)

        if extra_axes_metadata is None:
            extra_axes_metadata = []

        if metadata is None:
            metadata = {}

        self._extra_axes_metadata = extra_axes_metadata
        self._metadata = metadata

        self._accumulated_defocus = accumulated_defocus
        self._partitions = partitions

        self._normalization = normalization

        self._crop_offset = crop_offset
        self._uncropped_gpts = uncropped_gpts
        self._check_axes_metadata()

    def __len__(self) -> int:
        return len(self.wave_vectors)

    @property
    def normalization(self) -> str:
        return self._normalization

    @property
    def full_wave_vectors(self):
        return prism_wave_vectors(self.planewave_cutoff, self.extent, self.energy, self.interpolation)

    @property
    def crop_offset(self) -> Tuple[int, int]:
        return self._crop_offset

    @property
    def uncropped_gpts(self) -> Tuple[int, int]:
        if self._uncropped_gpts is None:
            return self.gpts
        return self._uncropped_gpts

    @property
    def is_cropped(self) -> bool:
        return self.uncropped_gpts != self.gpts

    @property
    def planewave_cutoff(self) -> float:
        return self._planewave_cutoff

    @property
    def num_axes(self) -> int:
        return len(self.array.shape)

    @property
    def num_base_axes(self) -> int:
        return 3

    @property
    def chunks(self) -> int:
        return self.array.chunks[:-2]

    @property
    def metadata(self) -> Dict:
        return self._metadata

    @property
    def accumulated_defocus(self) -> float:
        return self._accumulated_defocus

    @accumulated_defocus.setter
    def accumulated_defocus(self, value):
        self._accumulated_defocus = value

    @property
    def wave_vectors(self) -> np.ndarray:
        """The spatial frequencies of each wave in the plane wave expansion."""
        return self._wave_vectors

    @property
    def interpolation(self) -> Tuple[int, int]:
        """Interpolation factor."""
        return self._interpolation

    @property
    def partitions(self) -> int:
        return self._partitions

    @property
    def interpolated_gpts(self) -> Tuple[int, int]:
        return self.uncropped_gpts[0] // self.interpolation[0], self.uncropped_gpts[1] // self.interpolation[1]

    def rechunk(self, chunks: int = None, **kwargs):
        if not isinstance(self.array, da.core.Array):
            raise RuntimeError()

        if chunks is None:
            chunks = self.array.chunks[:-3] + ((sum(self.array.chunks[-3]),),) + self.array.chunks[-2:]

        self._array = self._array.rechunk(chunks=chunks, **kwargs)
        return self

    def crop_to_positions(self, positions: Union[np.ndarray, AbstractScan]):
        xp = get_array_module(self.array)
        if self.interpolation == (1, 1):
            corner = (0, 0)
            cropped_array = self.array
        else:
            corner, size, _ = _minimum_crop(positions, self.sampling, self.interpolated_gpts)
            corner = (corner[0] if self.interpolation[0] > 1 else 0, corner[1] if self.interpolation[1] > 1 else 0)

            size = (size[0] if self.interpolation[0] > 1 else self.gpts[0],
                    size[1] if self.interpolation[1] > 1 else self.gpts[1])

            if self.is_lazy:
                cropped_array = self.array.map_blocks(wrapped_crop_2d,
                                                      corner=corner,
                                                      size=size,
                                                      chunks=self.array.chunks[:-2] + ((size[0],), (size[1],)),
                                                      meta=xp.array((), dtype=xp.complex64))
            else:
                cropped_array = wrapped_crop_2d(self.array, corner=corner, size=size)

        d = self._copy_as_dict(copy_array=False)
        d['array'] = cropped_array
        d['crop_offset'] = corner
        d['uncropped_gpts'] = self.uncropped_gpts
        return self.__class__(**d)

    def downsample(self, max_angle: Union[str, float] = 'cutoff') -> 'SMatrixArray':
        waves = Waves(self.array, sampling=self.sampling, energy=self.energy,
                      extra_axes_metadata=self.axes_metadata[:-2])

        if self.normalization == 'probe':
            waves = waves.downsample(max_angle=max_angle, normalization='amplitude')
        elif self.normalization == 'planewaves':
            waves = waves.downsample(max_angle=max_angle, normalization='values')
        else:
            raise RuntimeError()

        d = self._copy_as_dict(copy_array=False)
        d['array'] = waves.array
        d['sampling'] = waves.sampling
        return self.__class__(**d)

    def streaming_multislice(self, potential, chunks=None, **kwargs):

        for chunk_start, chunk_stop in generate_chunks(len(self), chunks=chunks):
            extra_axes_metadata = self.extra_axes_metadata + [PrismPlaneWavesAxis()]
            waves = Waves(self.array[chunk_start:chunk_stop], energy=self.energy, sampling=self.sampling,
                          extra_axes_metadata=extra_axes_metadata)
            waves = waves.copy('gpu')
            self._array[chunk_start:chunk_stop] = waves.multislice(potential, **kwargs).copy('cpu').array

        return self

    def multislice(self, potential: Union[Atoms, AbstractPotential], chunks: int = None, **kwargs) -> 'SMatrixArray':
        """
        Propagate the scattering matrix through the provided potential.

        Parameters
        ----------
        potential : AbstractPotential object
            Scattering potential.

        Returns
        -------
        Waves object.
            Probe exit wave functions for the provided positions.
        """

        if chunks is None:
            chunks = len(self)

        if self._is_streaming:
            return self.streaming_multislice(potential, chunks)

        extra_axes_metadata = self.extra_axes_metadata + [PrismPlaneWavesAxis()]

        waves = Waves(self.array, energy=self.energy, sampling=self.sampling,
                      extra_axes_metadata=extra_axes_metadata)

        array = waves.multislice(potential, **kwargs).array

        d = self._copy_as_dict(copy_array=False)
        d['array'] = array
        return self.__class__(**d)

    def remove_tilt(self):
        xp = get_array_module(self.array)
        if self.is_lazy:
            array = self.array.map_blocks(remove_tilt,
                                          planewave_cutoff=self.planewave_cutoff,
                                          extent=self.extent,
                                          gpts=self.gpts,
                                          energy=self.energy,
                                          interpolation=self.interpolation,
                                          partitions=self.partitions,
                                          accumulated_defocus=self.accumulated_defocus,
                                          meta=xp.array((), dtype=xp.complex64))
        else:
            array = remove_tilt(self.array,
                                planewave_cutoff=self.planewave_cutoff,
                                extent=self.extent,
                                gpts=self.gpts,
                                energy=self.energy,
                                interpolation=self.interpolation,
                                partitions=self.partitions,
                                accumulated_defocus=self.accumulated_defocus)

        self._array = array
        return self

    def interpolate_full(self, chunks):
        xp = get_array_module(self.array)
        self.remove_tilt()
        self.rechunk()

        wave_vectors = prism_wave_vectors(self.planewave_cutoff, self.extent, self.energy, self.interpolation)

        arrays = []
        for start, end in generate_chunks(len(wave_vectors), chunks=chunks):
            array = dask.delayed(interpolate_full)(array=self.array,
                                                   parent_wave_vectors=self.wave_vectors,
                                                   wave_vectors=wave_vectors[start:end],
                                                   extent=self.extent,
                                                   gpts=self.gpts,
                                                   energy=self.energy,
                                                   defocus=self.accumulated_defocus)

            array = da.from_delayed(array, shape=(end - start,) + self.gpts, dtype=xp.complex64)
            array = array * np.sqrt(len(self) / len(wave_vectors))
            arrays.append(array)

        array = da.concatenate(arrays)
        d = self._copy_as_dict(copy_array=False)
        d['array'] = array
        d['wave_vectors'] = wave_vectors
        d['partitions'] = None
        return self.__class__(**d)

    def _validate_positions_per_reduction(self, positions_per_reduction):
        if positions_per_reduction == 'auto' or positions_per_reduction is None:
            positions_per_reduction = 300
        return positions_per_reduction

    def _get_s_matrices(self):

        if len(self.array.shape) == 3:
            return [self]

        s_matrices = []
        for i in range(len(self.array)):
            d = self._copy_as_dict(copy_array=False)
            d['array'] = self.array[i]
            d['extra_axes_metadata'] = []
            s_matrices.append(self.__class__(**d))

        return s_matrices

    @property
    def _is_stored_on_host(self):
        if hasattr(self.array, '_meta'):
            return isinstance(self.array._meta, np.ndarray)

        return isinstance(self.array, np.ndarray)

    @property
    def _is_streaming(self):
        return self._device == 'gpu' and self._is_stored_on_host

    def _apply_reduction_func(self, func, detectors, scan, **kwargs):
        detectors = validate_detectors(detectors)
        meta_waves = self.meta_waves

        new_cls = [detector.measurement_type(meta_waves, scan) for detector in detectors]
        new_cls_kwargs = [detector.measurement_kwargs(meta_waves, scan=scan) for detector in detectors]

        signatures = []
        output_sizes = {}
        meta = []
        i = 3
        for detector in detectors:
            shape = detector.measurement_shape(meta_waves, scan=scan)[self.num_extra_axes:]
            signatures.append(f'({",".join([str(i) for i in range(i, i + len(shape))])})')
            output_sizes.update({str(index): n for index, n in zip(range(i, i + len(shape)), shape)})
            meta.append(np.array((), dtype=detector.measurement_dtype))
            i += len(shape)

        signature = '(0,1,2)->' + ','.join(signatures)

        measurements = self.apply_gufunc(func,
                                         detectors=detectors,
                                         scan=scan,
                                         new_cls=new_cls,
                                         new_cls_kwargs=new_cls_kwargs,
                                         signature=signature,
                                         # axes=[(-3, -2, -1), (-1,-2)],
                                         output_sizes=output_sizes,
                                         allow_rechunk=True,
                                         meta=meta,
                                         **kwargs)

        return measurements

    def _generate_waves(self, scan, ctf, probes_per_reduction):
        xp = get_array_module(self._device)

        ctf = self._validate_ctf(ctf)
        scan = self._validate_positions(scan, ctf)
        probes_per_reduction = self._validate_positions_per_reduction(probes_per_reduction)

        if self.partitions:
            extent = (self.interpolated_gpts[0] * self.sampling[0],
                      self.interpolated_gpts[1] * self.sampling[1])

            wave_vectors = prism_wave_vectors(self.planewave_cutoff, extent, self.energy, (1, 1))

            ctf = ctf.copy()
            ctf.defocus = -self.accumulated_defocus

            basis = beamlet_basis(ctf,
                                  self.wave_vectors,
                                  wave_vectors,
                                  self.interpolated_gpts,
                                  self.sampling).astype(np.complex64)

        else:
            wave_vectors = xp.asarray(self.wave_vectors)
            alpha = xp.sqrt(wave_vectors[:, 0] ** 2 + wave_vectors[:, 1] ** 2) * self.wavelength
            phi = xp.arctan2(wave_vectors[:, 0], wave_vectors[:, 1])
            basis = ctf.evaluate(alpha, phi)

        for indices, positions in scan.generate_positions(chunks=probes_per_reduction):
            if self.partitions is None:
                waves = _reduce(self, basis, positions, scan.axes_metadata)
            else:
                waves = _reduce_partitioned(self, basis, positions, scan.axes_metadata)
            yield indices, waves

    def _distribute_reductions(self, detectors, scan, scan_divisions, ctf, positions_per_reduction):

        scans = scan.divide(scan_divisions)

        scans = [item for sublist in scans for item in sublist]

        measurements = []
        for scan in scans:
            cropped_s_matrix_array = self.crop_to_positions(scan)

            if self._is_streaming:
                cropped_s_matrix_array._array = cropped_s_matrix_array._array.map_blocks(cp.asarray)

            measurement = cropped_s_matrix_array._apply_reduction_func(reduce_s_matrix,
                                                                       detectors=detectors,
                                                                       scan=scan,
                                                                       ctf=ctf,
                                                                       positions_per_reduction=positions_per_reduction)
            measurements.append(measurement)

        measurements = list(map(list, zip(*measurements)))

        for i, measurement in enumerate(measurements):
            cls = measurement[0].__class__
            kwargs = measurement[0]._copy_as_dict(copy_array=False)

            measurement = [measurement[i:i + scan_divisions[0]] for i in
                           range(0, len(measurement), scan_divisions[0])]

            array = np.concatenate([np.concatenate([item.array for item in block], axis=1) for block in measurement],
                                   axis=0)
            kwargs['array'] = array
            measurements[i] = cls(**kwargs)

        return measurements

    def reduce(self,
               detectors: Union[AbstractDetector, List[AbstractDetector]] = None,
               positions: Union[np.ndarray, AbstractScan] = None,
               ctf: Union[CTF, Dict] = None,
               distribute_scan: Union[int, Tuple[int, int]] = False,
               probes_per_reduction: int = None) -> Union[Waves, AbstractMeasurement, List[AbstractMeasurement]]:

        """
        Scan the probe across the potential and record a measurement for each detector.

        Parameters
        ----------
        detectors : List of Detector objects
            The detectors recording the measurements.
        positions : Scan object
            Scan defining the positions of the probe wave functions.
        ctf: CTF object, optional
            The probe contrast transfer function. Default is None (aperture is set by the planewave cutoff).
        distribute_scan : two int, optional
            Partitioning of the scan. The scattering matrix will be reduced in similarly partitioned chunks.
            Should be equal to or greater than the interpolation.
        probes_per_reduction : int, optional
            Number of positions per reduction operation. To utilize thread
        """

        positions_per_reduction = self._validate_positions_per_reduction(probes_per_reduction)
        detectors = validate_detectors(detectors)
        ctf = self._validate_ctf(ctf)
        scan = self._validate_positions(positions, ctf)

        measurements_ensemble = []
        for i, s_matrix in enumerate(self._get_s_matrices()):
            if not distribute_scan:
                measurement = s_matrix._apply_reduction_func(reduce_s_matrix,
                                                             detectors=detectors,
                                                             scan=scan,
                                                             ctf=ctf,
                                                             positions_per_reduction=positions_per_reduction)


            else:
                measurement = s_matrix._distribute_reductions(detectors,
                                                              scan,
                                                              distribute_scan,
                                                              ctf,
                                                              positions_per_reduction)

            measurements_ensemble.append(measurement)

            # if max_concurrent is not None:
            #     if i >= max_concurrent:
            #         measurement = graph_manipulation.bind(measurement, measurements[i - max_concurrent])
            # else:
            #     measurement = graph_manipulation.wait_on(measurement)
            #
            # measurements.append(measurement)

        measurements_ensemble = list(map(list, zip(*measurements_ensemble)))
        return stack_measurement_ensembles(detectors, measurements_ensemble)

    def _copy_as_dict(self, copy_array: bool = True):
        d = {'energy': self.energy,
             'wave_vectors': self.wave_vectors.copy(),
             'interpolation': self.interpolation,
             'planewave_cutoff': self.planewave_cutoff,
             'sampling': self.sampling,
             'accumulated_defocus': self.accumulated_defocus,
             'crop_offset': self.crop_offset,
             'uncropped_gpts': self._uncropped_gpts,
             'tilt': self.tilt,
             'partitions': self.partitions,
             'antialias_cutoff_gpts': self.antialias_cutoff_gpts,
             'device': self._device,
             'extra_axes_metadata': deepcopy(self._extra_axes_metadata),
             'metadata': copy(self.metadata)}

        if copy_array:
            d['array'] = self.array.copy()
        return d

    def copy(self, device: str = None):
        """Make a copy."""
        d = self._copy_as_dict(copy_array=False)

        if device is not None:
            array = copy_to_device(self.array, device)
        else:
            array = self.array.copy()

        d['array'] = array
        return self.__class__(**d)


class SMatrix(AbstractSMatrix):
    """
    Scattering matrix builder class

    The scattering matrix builder object is used for creating scattering matrices and simulating STEM experiments using
    the PRISM algorithm.

    Parameters
    ----------
    potential : Atoms or Potential
    energy : float
        Electron energy [eV].
    planewave_cutoff : float
        The angular cutoff of the plane wave expansion [mrad].
    interpolation : one or two int, optional
        Interpolation factor. Default is 1 (no interpolation).
    extent : one or two float, optional
        Lateral extent of wave functions [Å]. Default is None (inherits the extent from the potential).
    gpts : one or two int, optional
        Number of grid points describing the wave functions. Default is None (inherits the gpts from the potential).
    sampling : one or two float, None
        Lateral sampling of wave functions [1 / Å]. Default is None (inherits the sampling from the potential).
    chunks :
    tilt : two float
        Small angle beam tilt [mrad].
    device : str, optional
        The calculations will be carried out on this device. Default is 'cpu'.
    """

    def __init__(self,
                 potential: Union[Atoms, AbstractPotential] = None,
                 energy: float = None,
                 planewave_cutoff: float = 30.,
                 interpolation: Union[int, Tuple[int, int]] = 1,
                 partitions: int = None,
                 normalize: bool = True,
                 extent: Union[float, Tuple[float, float]] = None,
                 gpts: Union[int, Tuple[int, int]] = None,
                 sampling: Union[float, Tuple[float, float]] = None,
                 chunks: int = None,
                 tilt: Tuple[float, float] = None,
                 device: str = None,
                 store_on_host: bool = False):

        self._device = _validate_device(device)
        self._grid = Grid(extent=extent, gpts=gpts, sampling=sampling)

        self._potential = validate_potential(potential, self)

        if potential is not None:
            self._grid = self._potential.grid

        self._interpolation = validate_interpolation(interpolation)
        self._planewave_cutoff = planewave_cutoff

        self._accelerator = Accelerator(energy=energy)
        self._beam_tilt = BeamTilt(tilt=tilt)
        self._partitions = partitions

        self._normalize = normalize
        self._chunks = chunks
        self._store_on_host = store_on_host

        self._extra_axes_metadata = []
        self._antialias_cutoff_gpts = None

    @property
    def metadata(self):
        return {'energy': self.energy}

    @property
    def shape(self):
        return (len(self),) + self.gpts

    @property
    def potential(self) -> AbstractPotential:
        return self._potential

    @potential.setter
    def potential(self, potential):
        self._potential = potential
        self._grid = potential.grid

    @property
    def chunks(self) -> int:
        if self._chunks is None:
            chunk_size = dask.utils.parse_bytes(dask.config.get('array.chunk-size'))
            chunks = int(chunk_size / self._bytes_per_wave())
        elif isinstance(self._chunks, int):
            chunks = self._chunks
        else:
            raise RuntimeError()

        return chunks

    @property
    def normalize(self):
        return self._normalize

    @property
    def planewave_cutoff(self) -> float:
        """Plane wave expansion cutoff."""
        return self._planewave_cutoff

    @planewave_cutoff.setter
    def planewave_cutoff(self, value: float):
        self._planewave_cutoff = value

    @property
    def interpolation(self) -> Tuple[int, int]:
        """Interpolation factor."""
        return self._interpolation

    @property
    def interpolated_gpts(self) -> Tuple[int, int]:
        return self.gpts[0] // self.interpolation[0], self.gpts[1] // self.interpolation[0]

    @property
    def partitions(self):
        return self._partitions

    def _build_chunk(self,
                     chunk_start: int = 0,
                     chunk_stop: int = None,
                     start: int = 0,
                     stop: int = None,
                     downsample: bool = False) -> np.ndarray:

        xp = get_array_module(self._device)

        if self.partitions is None:
            wave_vectors = prism_wave_vectors(self.planewave_cutoff, self.extent, self.energy, self.interpolation, xp)

        else:
            wave_vectors = partitioned_prism_wave_vectors(self.planewave_cutoff, self.extent, self.energy,
                                                          self.partitions, num_points_per_ring=6, xp=xp)

        if chunk_stop is None:
            chunk_stop = len(wave_vectors)

        array = plane_waves(wave_vectors[chunk_start:chunk_stop], self.extent, self.gpts)

        if self.normalize:
            normalization_constant = np.prod(self.gpts) * xp.sqrt(len(wave_vectors)) / np.prod(self.interpolation)
            array = array / normalization_constant.astype(xp.float32)
        else:
            array = array / xp.sqrt(np.prod(self.gpts).astype(xp.float32))

        if self.potential is not None:
            waves = Waves(array, extent=self.extent, energy=self.energy, extra_axes_metadata=[OrdinalAxis()])
            waves = multislice(waves, self.potential, start=start, stop=stop)

            if downsample:
                waves = waves.downsample(max_angle=downsample)

            return waves.array
        else:
            return array

    def _generate_s_matrices(self,
                             lazy: bool = None,
                             downsample: Union[float, str] = False,
                             start: int = 0,
                             stop: int = None) -> List[SMatrixArray]:

        lazy = validate_lazy(lazy)

        if self.potential:
            self.grid.match(self.potential)
        self.grid.check_is_defined()

        xp = get_array_module(self._device)

        def _s_matrix_configuration(potential):
            d = self._copy_as_dict(copy_potential=False)
            d['potential'] = potential
            return self.__class__(**d)

        def _build_chunk(s_matrix, chunk_start, chunk_stop, start, stop, downsample):
            return s_matrix._build_chunk(chunk_start, chunk_stop, start, stop, downsample)

        if downsample:
            gpts = self._gpts_within_angle(downsample)
        else:
            gpts = self.gpts

        sampling = (self.extent[0] / gpts[0], self.extent[1] / gpts[1])

        if self.potential is None:
            potentials = [None]
        else:
            potentials = self.potential.get_potential_distribution(lazy=lazy)

        for potential in potentials:
            if lazy:
                s_matrix = dask.delayed(_s_matrix_configuration)(potential)
            else:
                s_matrix = _s_matrix_configuration(potential)

            arrays = []
            for chunk_start, chunk_stop in generate_chunks(len(self), chunks=self.chunks):
                if lazy:
                    array = dask.delayed(_build_chunk)(s_matrix, chunk_start, chunk_stop, start, stop, downsample)
                    array = da.from_delayed(array,
                                            shape=(chunk_stop - chunk_start,) + gpts,
                                            meta=xp.array((), dtype=xp.complex64))
                else:
                    array = _build_chunk(s_matrix, chunk_start, chunk_stop, start, stop, downsample)

                if self._store_on_host:
                    if lazy:
                        array = array.map_blocks(cp.asnumpy)
                    else:
                        with cp.cuda.Stream():
                            array = cp.asnumpy(array)

                arrays.append(array)

            if lazy:
                array = da.concatenate(arrays)
            else:
                array = xp.concatenate(arrays)

            s_matrix = SMatrixArray(array,
                                    interpolation=self.interpolation,
                                    planewave_cutoff=self.planewave_cutoff,
                                    sampling=sampling,
                                    energy=self.energy,
                                    tilt=self.tilt,
                                    partitions=self.partitions,
                                    wave_vectors=self.wave_vectors,
                                    antialias_cutoff_gpts=self.antialias_cutoff_gpts,
                                    device=self._device,
                                    metadata=self.metadata)

            # print(gpts)
            # print(sampling, self.sampling)
            # print(s_matrix.cutoff_angles, self.cutoff_angles)
            # sss
            if self.potential is not None:
                s_matrix.accumulated_defocus = self.potential.thickness

            yield s_matrix

    def multislice(self,
                   potential: Union[Atoms, AbstractPotential] = None,
                   start: int = 0,
                   stop: int = None,
                   lazy: bool = None,
                   downsample: bool = False):
        if potential is not None:
            potential = validate_potential(potential, self)
            self.grid.match(potential)
            self.potential = potential
        return self.build(start=start, stop=stop, lazy=lazy, downsample=downsample)

    def build(self,
              start: int = 0,
              stop: int = None,
              lazy: bool = None,
              downsample: Union[float, str] = False) -> SMatrixArray:
        """
        Build the plane waves of the scattering matrix and propagate the waves through the potential using the
        multislice algorithm.

        Parameters
        ----------
        start : int
            First slice index for running the multislice algorithm. Default is first slice of the potential.
        stop : int
            Last slice for running the multislice algorithm. If smaller than start the multislice algorithm will run
            in the reverse direction. Default is last slice of the potential.
        lazy : bool
            If True, build the scattering matrix lazily with dask array.
        downsample : float or str or False
            If not False, the scattering matrix is downsampled to a maximum given scattering angle after running the
            multislice algorithm. If downsample is given as a float angle may be given as a float

            is given the scattering matrix is downsampled to a maximum scattering angle

        Returns
        -------
        SMatrixArray
        """

        generator = self._generate_s_matrices(lazy=lazy, start=start, stop=stop, downsample=downsample)
        s_matrices = [s_matrix for s_matrix in generator]

        if len(s_matrices) > 1:
            return stack_s_matrices(s_matrices, FrozenPhononsAxis())
        else:
            return s_matrices[0]

    def _validate_ctf(self, ctf):
        if ctf is None:
            ctf = CTF(energy=self.energy, semiangle_cutoff=self.planewave_cutoff)

        if ctf.semiangle_cutoff is None:
            ctf.semiangle_cutoff = self.planewave_cutoff

        ctf.accelerator.check_is_defined()
        return ctf

    def scan(self,
             detectors: Union[AbstractDetector, List[AbstractDetector]] = None,
             scan: Union[np.ndarray, AbstractScan] = None,
             ctf: Union[CTF, Dict] = None,
             distribute_scan: Union[bool, Tuple[int, int]] = False,
             probes_per_reduction: int = None,
             downsample: Union[float, str] = 'cutoff',
             lazy: bool = None) -> Union[Waves, AbstractMeasurement, List[AbstractMeasurement]]:

        """
        Scan the probe across the potential and record a measurement for each detector.

        Parameters
        ----------
        detectors : detector and list of Detector
            The detectors recording the measurements.
        scan : Scan object
            Scan defining the positions of the probe wave functions.
        ctf: CTF object, optional
            The probe contrast transfer function. Default is None (aperture is set by the planewave cutoff).
        distribute_scan : two int, optional
            Partitioning of the scan. The scattering matrix will be reduced in similarly partitioned chunks.
            Should be equal to or greater than the interpolation.
        probes_per_reduction : int, optional
            Number of probe positions per reduction operation.
        lazy : bool

        """

        lazy = validate_lazy(lazy)
        ctf = self._validate_ctf(ctf)
        scan = validate_scan(scan, ctf, self.extent)

        if lazy:
            return self.build(lazy=lazy, downsample=downsample).reduce(detectors=detectors,
                                                                       positions=scan,
                                                                       ctf=ctf,
                                                                       distribute_scan=distribute_scan,
                                                                       probes_per_reduction=probes_per_reduction)

        detectors = validate_detectors(detectors)
        s_matrix_generator = self._generate_s_matrices(lazy=lazy, downsample=downsample)

        # print(detectors[0].measurement_shape(self.equivalent_probe()))

        measurements = []
        for i, s_matrix in enumerate(s_matrix_generator):
            waves_generator = s_matrix._generate_waves(scan, ctf, probes_per_reduction)
            # print(self.cutoff_angles)
            # print(s_matrix.cutoff_angles)
            # sss
            if i == 0:
                meta_waves = s_matrix.meta_waves
                measurements = allocate_measurements(meta_waves, scan, detectors, self.potential)

            for indices, exit_waves in waves_generator:

                # print(exit_waves.cutoff_angles, probe.cutoff_angles, self.cutoff_angles)

                for detector, measurement in zip(detectors, measurements):

                    new_measurements = detector.detect(exit_waves)

                    if measurement.frozen_phonon_axes:
                        measurement.array[(i,) + indices] += new_measurements.array
                    else:
                        measurement.array[indices] += new_measurements.array

        if len(measurements) == 1:
            return measurements[0]
        else:
            return ComputableList(measurements)

    def __len__(self) -> int:
        return len(self.wave_vectors)

    def linear_scaling_transition_scan(self, scan, collection_angle, transitions, ctf: CTF = None,
                                       reverse_multislice=False, lazy=False):
        d = self._copy_as_dict(copy_potential=False)
        d['potential'] = self.potential
        d['planewave_cutoff'] = collection_angle
        S2 = self.__class__(**d)

        ctf = self._validate_ctf(ctf)
        scan = self._validate_positions(positions=scan, ctf=ctf)

        if hasattr(transitions, 'get_transition_potentials'):
            if lazy:
                transitions = dask.delayed(transitions.get_transition_potentials)()
            else:
                transitions = transitions.get_transition_potentials()

        return linear_scaling_transition_multislice(self, S2, scan, transitions, reverse_multislice=reverse_multislice)

    @property
    def wave_vectors(self) -> np.ndarray:
        self.grid.check_is_defined()
        self.accelerator.check_is_defined()
        if self._store_on_host:
            xp = np
        else:
            xp = get_array_module(self._device)

        if self.partitions is None:
            wave_vectors = prism_wave_vectors(self.planewave_cutoff, self.extent, self.energy, self.interpolation,
                                              xp=xp)

        else:
            wave_vectors = partitioned_prism_wave_vectors(self.planewave_cutoff, self.extent, self.energy,
                                                          num_rings=self.partitions, xp=xp)

        return wave_vectors

    def _copy_as_dict(self, copy_potential: bool = True):
        potential = self.potential
        if copy_potential and self.potential is not None:
            potential = potential.copy()
        d = {'potential': potential,
             'energy': self.energy,
             'planewave_cutoff': self.planewave_cutoff,
             'interpolation': self.interpolation,
             'partitions': self.partitions,
             'normalize': self.normalize,
             'extent': self.extent,
             'gpts': self.gpts,
             'sampling': self.sampling,
             'chunks': self.chunks,
             'store_on_host': self._store_on_host,
             'tilt': self.tilt,
             'device': self._device}
        return d

    def __copy__(self) -> 'SMatrix':
        return self.__class__(**self._copy_as_dict())

    def copy(self) -> 'SMatrix':
        """Make a copy."""
        return copy(self)
