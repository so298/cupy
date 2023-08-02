import cupy
import cupyx.scipy.ndimage

from cupyx.scipy.signal._iir_utils import apply_iir_sos
from cupyx.scipy.interpolate._bspline import BSpline


def sepfir2d(input, hrow, hcol):
    """Convolve with a 2-D separable FIR filter.

    Convolve the rank-2 input array with the separable filter defined by the
    rank-1 arrays hrow, and hcol. Mirror symmetric boundary conditions are
    assumed. This function can be used to find an image given its B-spline
    representation.

    The arguments `hrow` and `hcol` must be 1-dimensional and of off length.

    Args:
        input (cupy.ndarray): The input signal
        hrow (cupy.ndarray): Row direction filter
        hcol (cupy.ndarray): Column direction filter

    Returns:
        cupy.ndarray: The filtered signal

    .. seealso:: :func:`scipy.signal.sepfir2d`
    """
    if any(x.ndim != 1 or x.size % 2 == 0 for x in (hrow, hcol)):
        raise ValueError('hrow and hcol must be 1 dimensional and odd length')
    dtype = input.dtype
    if dtype.kind == 'c':
        dtype = cupy.complex64 if dtype == cupy.complex64 else cupy.complex128
    elif dtype == cupy.float32 or dtype.itemsize <= 2:
        dtype = cupy.float32
    else:
        dtype = cupy.float64
    input = input.astype(dtype, copy=False)
    hrow = hrow.astype(dtype, copy=False)
    hcol = hcol.astype(dtype, copy=False)
    filters = (hcol[::-1].conj(), hrow[::-1].conj())
    return cupyx.scipy.ndimage._filters._run_1d_correlates(
        input, (0, 1), lambda i: filters[i], None, 'reflect', 0)


def _quadratic(x):
    x = abs(cupy.asarray(x, dtype=float))
    b = BSpline.basis_element(
        cupy.asarray([-1.5, -0.5, 0.5, 1.5]), extrapolate=False)
    out = b(x)
    out[(x < -1.5) | (x > 1.5)] = 0
    return out


def _cubic(x):
    x = cupy.asarray(x, dtype=float)
    b = BSpline.basis_element(
        cupy.asarray([-2, -1, 0, 1, 2]), extrapolate=False)
    out = b(x)
    out[(x < -2) | (x > 2)] = 0
    return out


@cupy.fuse()
def _coeff_smooth(lam):
    xi = 1 - 96 * lam + 24 * lam * cupy.sqrt(3 + 144 * lam)
    omeg = cupy.arctan2(cupy.sqrt(144 * lam - 1), cupy.sqrt(xi))
    rho = (24 * lam - 1 - cupy.sqrt(xi)) / (24 * lam)
    rho = rho * cupy.sqrt(
        (48 * lam + 24 * lam * cupy.sqrt(3 + 144 * lam)) / xi)
    return rho, omeg


@cupy.fuse()
def _hc(k, cs, rho, omega):
    return (cs / cupy.sin(omega) * (rho ** k) * cupy.sin(omega * (k + 1)) *
            cupy.greater(k, -1))


@cupy.fuse()
def _hs(k, cs, rho, omega):
    c0 = (cs * cs * (1 + rho * rho) / (1 - rho * rho) /
          (1 - 2 * rho * rho * cupy.cos(2 * omega) + rho ** 4))
    gamma = (1 - rho * rho) / (1 + rho * rho) / cupy.tan(omega)
    ak = cupy.abs(k)
    return c0 * rho ** ak * (
        cupy.cos(omega * ak) + gamma * cupy.sin(omega * ak))


def _cubic_smooth_coeff(signal, lamb):
    rho, omega = _coeff_smooth(lamb)
    cs = 1 - 2 * rho * cupy.cos(omega) + rho * rho
    K = len(signal)
    yp = cupy.zeros((K,), signal.dtype.char)
    k = cupy.arange(K)

    state_0 = (_hc(0, cs, rho, omega) * signal[0] +
               cupy.sum(_hc(k + 1, cs, rho, omega) * signal))
    state_1 = (_hc(0, cs, rho, omega) * signal[0] +
               _hc(1, cs, rho, omega) * signal[1] +
               cupy.sum(_hc(k + 2, cs, rho, omega) * signal))

    zi = cupy.r_[0, 0, state_0, state_1]
    zi = cupy.atleast_2d(zi)

    coef = cupy.r_[cs, 0, 0, 1, -2 * rho * cupy.cos(omega), rho * rho]
    coef = cupy.atleast_2d(coef)

    # Forward pass:
    #
    # yp[0] = (_hc(0, cs, rho, omega) * signal[0] +
    #          cupy.sum(_hc(k + 1, cs, rho, omega) * signal))
    # yp[1] = (_hc(0, cs, rho, omega) * signal[0] +
    #          _hc(1, cs, rho, omega) * signal[1] +
    #          cupy.sum(_hc(k + 2, cs, rho, omega) * signal))
    # for n in range(2, K):
    #     yp[n] = (cs * signal[n] + 2 * rho * cupy.cos(omega) * yp[n - 1] -
    #              rho * rho * yp[n - 2])

    yp, _ = apply_iir_sos(signal[2:], coef, zi=zi, dtype=signal.dtype)
    yp = cupy.r_[state_0, state_1, yp]

    # Reverse pass:
    #
    # y[K - 1] = cupy.sum((_hs(k, cs, rho, omega) +
    #                      _hs(k + 1, cs, rho, omega)) * signal[::-1])
    # y[K - 2] = cupy.sum((_hs(k - 1, cs, rho, omega) +
    #                      _hs(k + 2, cs, rho, omega)) * signal[::-1])
    # for n in range(K - 3, -1, -1):
    #     y[n] = (cs * yp[n] + 2 * rho * cupy.cos(omega) * y[n + 1] -
    #             rho * rho * y[n + 2])

    state_0 = cupy.sum((_hs(k, cs, rho, omega) +
                        _hs(k + 1, cs, rho, omega)) * signal[::-1])
    state_1 = cupy.sum((_hs(k - 1, cs, rho, omega) +
                        _hs(k + 2, cs, rho, omega)) * signal[::-1])

    zi = cupy.r_[0, 0, state_0, state_1]
    zi = cupy.atleast_2d(zi)

    y, _ = apply_iir_sos(yp[-3::-1], coef, zi=zi, dtype=signal.dtype)
    y = cupy.r_[y[::-1], state_1, state_0]
    return y


def _cubic_coeff(signal):
    zi = -2 + cupy.sqrt(3)
    K = len(signal)
    powers = zi ** cupy.arange(K)

    if K == 1:
        yplus = signal[0] + zi * cupy.sum(powers * signal)
        output = zi / (zi - 1) * yplus
        return cupy.atleast_1d(output)

    state = cupy.r_[0, 0, 0, cupy.sum(powers * signal)]
    state = cupy.atleast_2d(state)
    coef = cupy.r_[1, 0, 0, 1, -zi, 0]
    coef = cupy.atleast_2d(coef)

    # yplus[0] = signal[0] + zi * sum(powers * signal)
    # for k in range(1, K):
    #     yplus[k] = signal[k] + zi * yplus[k - 1]
    yplus, _ = apply_iir_sos(signal, coef, zi=state, apply_fir=False,
                             dtype=signal.dtype)

    out_last = zi / (zi - 1) * yplus[K - 1]
    state = cupy.r_[0, 0, 0, out_last]
    state = cupy.atleast_2d(state)

    coef = cupy.r_[-zi, 0, 0, 1, -zi, 0]
    coef = cupy.atleast_2d(coef)

    # output[K - 1] = zi / (zi - 1) * yplus[K - 1]
    # for k in range(K - 2, -1, -1):
    #     output[k] = zi * (output[k + 1] - yplus[k])
    output, _ = apply_iir_sos(
        yplus[-2::-1], coef, zi=state, dtype=signal.dtype)
    output = cupy.r_[output[::-1], out_last]
    return output * 6.0


def _quadratic_coeff(signal):
    zi = -3 + 2 * cupy.sqrt(2.0)
    K = len(signal)
    powers = zi ** cupy.arange(K)

    if K == 1:
        yplus = signal[0] + zi * cupy.sum(powers * signal)
        output = zi / (zi - 1) * yplus
        return cupy.atleast_1d(output)

    state = cupy.r_[0, 0, 0, cupy.sum(powers * signal)]
    state = cupy.atleast_2d(state)
    coef = cupy.r_[1, 0, 0, 1, -zi, 0]
    coef = cupy.atleast_2d(coef)

    # yplus[0] = signal[0] + zi * cupy.sum(powers * signal)
    # for k in range(1, K):
    #     yplus[k] = signal[k] + zi * yplus[k - 1]
    yplus, _ = apply_iir_sos(signal, coef, zi=state, apply_fir=False,
                             dtype=signal.dtype)

    out_last = zi / (zi - 1) * yplus[K - 1]
    state = cupy.r_[0, 0, 0, out_last]
    state = cupy.atleast_2d(state)

    coef = cupy.r_[-zi, 0, 0, 1, -zi, 0]
    coef = cupy.atleast_2d(coef)

    # output[K - 1] = zi / (zi - 1) * yplus[K - 1]
    # for k in range(K - 2, -1, -1):
    #     output[k] = zi * (output[k + 1] - yplus[k])
    output, _ = apply_iir_sos(
        yplus[-2::-1], coef, zi=state, dtype=signal.dtype)
    output = cupy.r_[output[::-1], out_last]
    return output * 8.0


def cspline1d(signal, lamb=0.0):
    """
    Compute cubic spline coefficients for rank-1 array.

    Find the cubic spline coefficients for a 1-D signal assuming
    mirror-symmetric boundary conditions. To obtain the signal back from the
    spline representation mirror-symmetric-convolve these coefficients with a
    length 3 FIR window [1.0, 4.0, 1.0]/ 6.0 .

    Parameters
    ----------
    signal : ndarray
        A rank-1 array representing samples of a signal.
    lamb : float, optional
        Smoothing coefficient, default is 0.0.

    Returns
    -------
    c : ndarray
        Cubic spline coefficients.

    See Also
    --------
    cspline1d_eval : Evaluate a cubic spline at the new set of points.

    """
    if lamb != 0.0:
        return _cubic_smooth_coeff(signal, lamb)
    else:
        return _cubic_coeff(signal)


def qspline1d(signal, lamb=0.0):
    """Compute quadratic spline coefficients for rank-1 array.

    Parameters
    ----------
    signal : ndarray
        A rank-1 array representing samples of a signal.
    lamb : float, optional
        Smoothing coefficient (must be zero for now).

    Returns
    -------
    c : ndarray
        Quadratic spline coefficients.

    See Also
    --------
    qspline1d_eval : Evaluate a quadratic spline at the new set of points.

    Notes
    -----
    Find the quadratic spline coefficients for a 1-D signal assuming
    mirror-symmetric boundary conditions. To obtain the signal back from the
    spline representation mirror-symmetric-convolve these coefficients with a
    length 3 FIR window [1.0, 6.0, 1.0]/ 8.0 .

    """
    if lamb != 0.0:
        raise ValueError("Smoothing quadratic splines not supported yet.")
    else:
        return _quadratic_coeff(signal)


def cspline1d_eval(cj, newx, dx=1.0, x0=0):
    """Evaluate a cubic spline at the new set of points.

    `dx` is the old sample-spacing while `x0` was the old origin. In
    other-words the old-sample points (knot-points) for which the `cj`
    represent spline coefficients were at equally-spaced points of:

      oldx = x0 + j*dx  j=0...N-1, with N=len(cj)

    Edges are handled using mirror-symmetric boundary conditions.

    Parameters
    ----------
    cj : ndarray
        cublic spline coefficients
    newx : ndarray
        New set of points.
    dx : float, optional
        Old sample-spacing, the default value is 1.0.
    x0 : int, optional
        Old origin, the default value is 0.

    Returns
    -------
    res : ndarray
        Evaluated a cubic spline points.

    See Also
    --------
    cspline1d : Compute cubic spline coefficients for rank-1 array.

    """
    newx = (cupy.asarray(newx) - x0) / float(dx)
    res = cupy.zeros_like(newx, dtype=cj.dtype)
    if res.size == 0:
        return res
    N = len(cj)
    cond1 = newx < 0
    cond2 = newx > (N - 1)
    cond3 = ~(cond1 | cond2)
    # handle general mirror-symmetry
    res[cond1] = cspline1d_eval(cj, -newx[cond1])
    res[cond2] = cspline1d_eval(cj, 2 * (N - 1) - newx[cond2])
    newx = newx[cond3]
    if newx.size == 0:
        return res
    result = cupy.zeros_like(newx, dtype=cj.dtype)
    jlower = cupy.floor(newx - 2).astype(int) + 1
    for i in range(4):
        thisj = jlower + i
        indj = thisj.clip(0, N - 1)  # handle edge cases
        result += cj[indj] * _cubic(newx - thisj)
    res[cond3] = result
    return res


def qspline1d_eval(cj, newx, dx=1.0, x0=0):
    """Evaluate a quadratic spline at the new set of points.

    Parameters
    ----------
    cj : ndarray
        Quadratic spline coefficients
    newx : ndarray
        New set of points.
    dx : float, optional
        Old sample-spacing, the default value is 1.0.
    x0 : int, optional
        Old origin, the default value is 0.

    Returns
    -------
    res : ndarray
        Evaluated a quadratic spline points.

    See Also
    --------
    qspline1d : Compute quadratic spline coefficients for rank-1 array.

    Notes
    -----
    `dx` is the old sample-spacing while `x0` was the old origin. In
    other-words the old-sample points (knot-points) for which the `cj`
    represent spline coefficients were at equally-spaced points of::

      oldx = x0 + j*dx  j=0...N-1, with N=len(cj)

    Edges are handled using mirror-symmetric boundary conditions.

    """
    newx = (cupy.asarray(newx) - x0) / dx
    res = cupy.zeros_like(newx)
    if res.size == 0:
        return res
    N = len(cj)
    cond1 = newx < 0
    cond2 = newx > (N - 1)
    cond3 = ~(cond1 | cond2)
    # handle general mirror-symmetry
    res[cond1] = qspline1d_eval(cj, -newx[cond1])
    res[cond2] = qspline1d_eval(cj, 2 * (N - 1) - newx[cond2])
    newx = newx[cond3]
    if newx.size == 0:
        return res
    result = cupy.zeros_like(newx)
    jlower = cupy.floor(newx - 1.5).astype(int) + 1
    for i in range(3):
        thisj = jlower + i
        indj = thisj.clip(0, N - 1)  # handle edge cases
        result += cj[indj] * _quadratic(newx - thisj)
    res[cond3] = result
    return res
