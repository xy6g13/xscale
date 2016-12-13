"""Define functions for linear filtering that works on multi-dimensional
xarray.DataArray and xarray.Dataset objects.
"""
# Python 2/3 compatibility
from __future__ import absolute_import, division, print_function
# Internal
import copy
from collections import Iterable
# Numpy and scipy
import numpy as np
import scipy.signal as sig
import scipy.signal.windows as win
import scipy.ndimage as im
import scipy.special as spec
# Xarray and dask
from dask.diagnostics import ProgressBar
import xarray as xr
# Matplotlib
import pylab as plt
from matplotlib import gridspec
# Current package
from .. import utils
from ..plot.plot2d import spectrum2d_plot
from ..plot.plot1d import spectrum_plot


# ------------------------------------------------------------------------------
# First part : Definition of custom window functions
# ------------------------------------------------------------------------------
def _lanczos(n, fc=0.02):
	"""
	Compute the coefficients of a Lanczos window. For private use only.

	Parameters
	----------
	n : int
		Number of points in the output window, must be an odd integer.
	fc : float
		Cutoff frequency

	Returns
	-------
	w : ndarray
		The weights associated to the boxcar window
	"""
	if not n % 2 == 1:
		raise ValueError("n must an odd integer")
	k = np.arange(- n / 2 + 1, n / 2 + 1)
	# k = np.arange(0, n + 1)
	# w = (np.sin(2 * pi * fc * k) / (pi * k) *
	#     np.sin(pi * k / n) / (pi * k / n))
	w = (np.sin(2. * np.pi * fc * k) / (np.pi * k) *
	     np.sin(np.pi * k / (n / 2.)) / (np.pi * k / (n / 2.)))
	# Particular case where k=0
	w[n / 2] = 2. * fc
	return w

_local_window_dict = {'lanczos': _lanczos, 'lcz': _lanczos}

# ------------------------------------------------------------------------------
# First part : Definition of window classes for filtering
# ------------------------------------------------------------------------------

#@xr.register_dataset_accessor('win')
@xr.register_dataarray_accessor('window')
class Window(object):
	"""
	Class for all different type of windows
	"""

	_attributes = ['order', 'cutoff', 'window']

	def __init__(self, xarray_obj):
		self._obj = xarray_obj
		self.obj = xarray_obj
		self.n = None
		self.dims = None
		self.cutoff = None
		self.window = None
		self.order = None
		self.coefficients = 1.
		self.coords = []
		self._depth = dict()
		self.fnyq = dict()

	def __repr__(self):
		"""
		Provide a nice string representation of the window object
		"""
		# Function copied from xarray.core.rolling
		attrs = ["{k}->{v}".format(k=k, v=getattr(self, k))
		         for k in self._attributes if
		         getattr(self, k, None) is not None]
		return "{klass} [{attrs}]".format(klass=self.__class__.__name__,
		                                  attrs=', '.join(attrs))

	def set(self, n=None, dims=None, cutoff=None, window='boxcar', chunks=None):
		"""
		Set the different properties of the current window

        If the variable associated to the window object is a non-dask array,
        it will be converted to dask array. If it's a dask array, it will be
        rechunked to the given chunksizes.

        If neither chunks is not provided for one or more dimensions, chunk
        sizes along that dimension will not be updated; non-dask arrays will be
        converted into dask arrays with a single block.

		Parameters
		----------
		n : int, sequence or dict, optional
			The window order over the dimensions specified through a dictionary or through the ``dims`` parameters. If
			``n`` is ``None``, the window order is set to the total size of the corresponding dimensions according to
			the ``dims`` parameters

		dims : str or sequence, optional
			Names of the dimension associated to the window. If ``dims`` is  None, all the dimensions are taken.

		cutoff : float, sequence or dict, optional
			The window cutoff over the dimensions specified through a dictionary, or through the ``dims``
			parameters. If `cutoff`` is ``None``, the cutoff parameters will be not used in the design the window.

		window : string, tuple of string and parameter values, or dict
			Desired window to use. See scipy.signal.get_window for a list of windows and required parameters.

        chunks : int, tuple or dict, optional
            Chunk sizes along each dimension, e.g., ``5``, ``(5, 5)`` or ``{'x': 5, 'y': 5}``
		"""

		# Check and interpret n and dims parameters
		self.n, self.dims = utils.infer_n_and_dims(self._obj, n, dims)
		self.order = {di: nbw for nbw, di in zip(self.n, self.dims)}
		# Check and interpret cutoff parameter
		self.cutoff = _infer_arg(cutoff, self.dims)
		# Check and interpret cutoff parameter
		self.window = _infer_arg(window, self.dims, default_value='boxcar')
		# Check and interpret the window parameter
		self.obj = self._obj.chunk(chunks=chunks)

		# Reset attributes
		self.fnyq = dict()
		self.dx = dict()
		self.coefficients = 1.
		self._depth = dict()

		# TODO: Test the size of the chunks compared to n

		# Build the multi-dimensional window: the hard part
		for di in self.obj.dims:
			axis_num = self.obj.get_axis_num(di)
			if di in self.dims:
				self._depth[axis_num] = self.order[di] % 2
				dx = utils.get_dx(self.obj, di)
				self.dx[di] = dx
				self.fnyq[di] = 1. / (2. * dx)
				# Compute the coefficients associated to the window using scipy functions
				if self.cutoff[di] is None:
					# Use get_window if the cutoff is undefined
					coefficients1d = sig.get_window(self.window[di], self.order[di])
				else:
					# Use firwin if the cutoff is defined
					coefficients1d = sig.firwin(self.order[di], self.cutoff[di], window=self.window[di],
					                            nyq=self.fnyq[di])
				# Normalize the coefficients
				self.coefficients = np.outer(self.coefficients, coefficients1d)
				# TODO: Try to add the rotational convention using meshgrid, in complement to the outer product
				# TODO: check the order of dimension of the kernel compared to the DataArray/DataSet objects
				self.coefficients = self.coefficients.squeeze()
			else:
				self.coefficients = np.expand_dims(self.coefficients, axis=axis_num)


	def convolve(self, mode='reflect', weights=None, compute=True):
		"""Convolve the current window with the data

		Parameters
		----------
		mode : {'reflect', 'same', 'valid'}, optional

		weights :

		compute : bool, optional
			If True, the computation is performed after the dask graph have been made. If False, only the dask graph is
			made is the computation will be performed later on. The latter allows the integration into a larger dask
			graph, which could include other computational steps.

		Returns
		-------
		res : xarray.DataArray
			Return the filtered  the low-passed filtered
		"""
		# Check if the data has more dimensions than the window and add
		# extra-dimensions to the window if it is the case
		coeffs = self.coefficients / np.sum(self.coefficients)
		mask = self.obj.notnull()
		if weights is None:
			weights = im.convolve(mask.astype(float), coeffs, mode=mode)
		filled_data = self.obj.fillna(0.).data

		def conv(x):
			xf = im.convolve(x, coeffs, mode=mode)
			return xf

		data = filled_data.map_overlap(conv, depth=self._depth, boundary=mode, trim=True)
		if compute:
			with ProgressBar():
				out = data.compute()
		else:
			out = data
		res = xr.DataArray(out, dims=self.obj.dims, coords=self.obj.coords, name=self.obj.name) / weights

		return res.where(mask == 1)


	def tapper(self, overlap=0.):
		"""
		Do a tappering of the data using the current window

		Parameters
		----------
		overlap:

		Returns
		-------
		data_tappered : dask array
			The data tappered y the window

		Notes
		-----
		"""
		# TODO: Write the function
		raise NotImplementedError
		if compute:
			with ProgressBar():
				out = data.compute()
		else:
			out = data
		res = xr.DataArray(out, dims=self.obj.dims, coords=self.obj.coords, name=self.obj.name)
		return res

	def boundary_weights(self, mode='reflect', drop_dims=None):
		"""
		Compute the boundary weights

		Parameters
		----------
			mode:

			drop_dims:
				Specify dimensions along which the mask is constant

		Returns
		-------
		"""
		mask = self.obj.notnull()
		new_dims = [di for di in self.obj.dims if di not in drop_dims]
		new_coords = {di:self.obj[di] for di in drop_dims if di not in drop_dims}
		mask = mask.isel(**{di:0 for di in drop_dims})
		weights = im.convolve(mask.astype(float), self.coefficients, mode=mode)
		res = xr.DataArray(weights, dims=new_dims, coords=new_coords, name='boundary weights')
		return res.where(mask == 1)

	def plot(self, format = 'landscape'):
		"""
		Plot the weights distribution of the window and the associated
		spectrum (work only for 1D and 2D windows).
		"""
		nod = len(self.dims)
		if nod == 1:
			# Compute 1D spectral response
			spectrum = np.fft.fft(self.coefficients.squeeze(), 2048) / (len(self.coefficients.squeeze()) / 2.0)
			freq = np.linspace(-0.5, 0.5, len(spectrum))
			response = 20 * np.log10(np.abs(np.fft.fftshift(spectrum / abs(spectrum).max())))
			# Plot window properties
			fig, (ax1, ax2) = plt.subplots(nrows=1, ncols=2, figsize=(10, 5))
			# First plot: weight distribution
			n = self._depth.values()[0]
			ax1.plot(np.arange(-n, n + 1), self.coefficients.squeeze())
			ax1.set_xlim((-n, n))
			ax1.set_title("Window: " + self.name)
			ax1.set_ylabel("Amplitude")
			ax1.set_xlabel("Sample")
			# Second plot: frequency response
			ax2.plot(freq, response)
			ax2.axis([-0.5, 0.5, -120, 0])
			ax2.set_title("Frequency response of the " + self.name + " window")
			ax2.set_ylabel("Normalized magnitude [dB]")
			ax2.set_xlabel("Normalized frequency [cycles per sample]")
			ax2.grid(True)
			plt.tight_layout()
		elif nod == 2:
			# Compute 2D spectral response
			nx = self.n[0]
			ny = self.n[1]
			spectrum = (np.fft.fft2(self.coefficients.squeeze(), [1024, 1024]) /
			            (np.size(self.coefficients.squeeze()) / 2.0))
			response = np.abs(np.fft.fftshift(spectrum / abs(spectrum).max()))
			fx = np.fft.fftshift(np.fft.fftfreq(1024, self.dx[self.dims[0]]))
			fy = np.fft.fftshift(np.fft.fftfreq(1024, self.dx[self.dims[0]]))
			f2d = np.meshgrid(fy, fx)
			if  format == 'landscape':
				gs = gridspec.GridSpec(2, 4, width_ratios=[2, 1, 2, 1], height_ratios=[1, 2])
				plt.figure(figsize=(11.69, 8.27))
			elif format == 'portrait':
				plt.figure(figsize=(8.27, 11.69))
			# Weight disribution along x
			ax_nx = plt.subplot(gs[0])
			ax_nx.plot(np.arange(-nx, nx + 1), self.coefficients.squeeze()[:, ny])
			ax_nx.set_xlim((-nx, nx))
			# Weight disribution along y
			ax_nx = plt.subplot(gs[5])
			ax_nx.plot(self.coefficients.squeeze()[nx, :], np.arange(-ny, ny + 1))
			ax_nx.set_ylim((-ny, ny))
			# Full 2d weight distribution
			ax_n2d = plt.subplot(gs[4])
			nx2d, ny2d = np.meshgrid(np.arange(-nx, nx + 1), np.arange(-ny, ny + 1), indexing='ij')
			ax_n2d.pcolormesh(nx2d, ny2d, self.coefficients.squeeze())
			ax_n2d.set_xlim((-nx, nx))
			ax_n2d.set_ylim((-ny, ny))
			box = dict(facecolor='white', pad=10.0)
			ax_n2d.text(0.97, 0.97, r'$w(n_x,n_y)$', fontsize='x-large', bbox=box, transform=ax_n2d.transAxes,
			            horizontalalignment='right', verticalalignment='top')
			# Frequency response for fy = 0
			ax_fx = plt.subplot(gs[2])
			spectrum_plot(ax_fx, fx, response[:, 512].squeeze(),)
			# ax_fx.set_xlim(xlim)
			ax_fx.grid(True)
			ax_fx.set_ylabel(r'$R(f_x,0)$', fontsize=24)
			# Frequency response for fx = 0
			ax_fy = plt.subplot(gs[7])
			spectrum_plot(ax_fy, response[:, 512].squeeze(), fy)
			#ax_fy.set_ylim(ylim)
			ax_fy.grid(True)
			ax_fy.set_xlabel(r'$,R(0,f_y)$', fontsize=24)
			# Full 2D frequency response
			ax_2d = plt.subplot(gs[6])
			spectrum2d_plot(ax_2d, fx, fy, response, zlog=True)
			ax_2d.set_ylabel(r'$f_y$', fontsize=24)
			ax_2d.set_xlabel(r'$f_x$', fontsize=24)
			ax_2d.grid(True)
			box = dict(facecolor='white', pad=10.0)
			ax_2d.text(0.97, 0.97, r'$R(f_x,f_y)$', fontsize='x-large', bbox=box, transform=ax_2d.transAxes,
			           horizontalalignment='right', verticalalignment='top')
			plt.tight_layout()
		else:
			raise ValueError, "This number of dimension is not supported by the plot function"


def _infer_arg(arg, dims, default_value=None):
	"""Private function for inferring the cutoff dictionary"""
	new_arg = dict()
	if arg is None:
		new_arg = {di: default_value for di in dims}
	elif utils.is_scalar(arg):
		new_arg = {di: arg for di in dims}
	elif utils.is_dict_like(arg):
		for di in dims:
			try:
				new_arg[di] = arg[di]
			except:
				new_arg[di] = default_value
	elif isinstance(arg, Iterable) and not isinstance(arg, basestring):
		if len(dims) == 1:
			new_arg[dims[0]] = arg
		else:
			for i, di in enumerate(dims):
				try:
					new_arg[di] = arg[i]
				except:
					new_arg[di] = default_value
	else:
		raise TypeError("This type of option is not supported for the second argument")
	return new_arg