"""Color-space helpers converting Home Assistant / Philips Hue color fields into
the linear RGB triples expected by a UsdLuxLightAPI `inputs:color` attribute.

USD light colors are linear scene-referred values, while Home Assistant reports
colors either as CIE xy chromaticity (native Hue gamut), 0-255 sRGB (gamma-encoded),
or HSV-style hue/saturation. Each helper below returns an (r, g, b) tuple in the
0-1 linear range. Ported from omni.home.assistant's color_utils.py.
"""
from __future__ import annotations

import colorsys
from typing import Tuple


def srgb_to_linear(c: float) -> float:
    c = max(0.0, min(1.0, c))
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def rgb255_to_linear(rgb: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Home Assistant's `rgb_color` is 0-255 sRGB (gamma-encoded)."""
    r, g, b = rgb
    return (
        srgb_to_linear(r / 255.0),
        srgb_to_linear(g / 255.0),
        srgb_to_linear(b / 255.0),
    )


def hs_to_linear(hs: Tuple[float, float]) -> Tuple[float, float, float]:
    """Home Assistant's `hs_color` is [hue 0-360, saturation 0-100]. Value is
    treated as full (1.0) since brightness is driven separately via intensity."""
    hue, sat = hs
    r, g, b = colorsys.hsv_to_rgb((hue % 360.0) / 360.0, max(0.0, min(1.0, sat / 100.0)), 1.0)
    return (srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b))


def xy_to_linear(xy: Tuple[float, float]) -> Tuple[float, float, float]:
    """Home Assistant's `xy_color` is the CIE 1931 xy chromaticity Philips Hue
    bulbs report natively. Converts xyY (Y=1) -> XYZ -> linear sRGB (D65), then
    normalizes so the brightest channel is 1.0 (brightness is driven separately)."""
    x, y = xy
    if y <= 0.0:
        return (0.0, 0.0, 0.0)

    Y = 1.0
    X = (x / y) * Y
    Z = ((1.0 - x - y) / y) * Y

    r = 3.2406 * X - 1.5372 * Y - 0.4986 * Z
    g = -0.9689 * X + 1.8758 * Y + 0.0415 * Z
    b = 0.0557 * X - 0.2040 * Y + 1.0570 * Z

    r, g, b = max(0.0, r), max(0.0, g), max(0.0, b)
    peak = max(r, g, b, 1e-6)
    return (r / peak, g / peak, b / peak)


def kelvin_to_xy(kelvin: float) -> Tuple[float, float]:
    """Approximates the CIE 1931 (x, y) chromaticity of an ideal blackbody
    radiator at `kelvin`, valid 1667K-25000K (Kim et al., 2002 polynomial fit
    to the Planckian locus). Used only as a last-resort fallback when Home
    Assistant reports color_temp_kelvin with no rgb_color/xy_color/hs_color."""
    t = max(1667.0, min(25000.0, kelvin))
    if t <= 4000.0:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390

    if t <= 2222.0:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000.0:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483

    return (x, y)


def kelvin_to_linear(kelvin: float) -> Tuple[float, float, float]:
    """Blackbody-locus fallback: color_temp_kelvin -> linear RGB."""
    return xy_to_linear(kelvin_to_xy(kelvin))
