"""Minimal 2D affine transform shared by the DXF and VSDX parsers."""

import math
from dataclasses import dataclass


@dataclass
class Matrix:
    """2D affine: x' = a*x + c*y + e ; y' = b*x + d*y + f"""

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    def then(self, outer: "Matrix") -> "Matrix":
        """self applied first, then outer."""
        return Matrix(
            a=outer.a * self.a + outer.c * self.b,
            b=outer.b * self.a + outer.d * self.b,
            c=outer.a * self.c + outer.c * self.d,
            d=outer.b * self.c + outer.d * self.d,
            e=outer.a * self.e + outer.c * self.f + outer.e,
            f=outer.b * self.e + outer.d * self.f + outer.f,
        )

    def rotation_deg(self) -> float:
        return math.degrees(math.atan2(self.b, self.a))

    def scale_xy(self) -> tuple[float, float]:
        return math.hypot(self.a, self.b), math.hypot(self.c, self.d)


def rotation(deg: float) -> Matrix:
    rad = math.radians(deg)
    return Matrix(a=math.cos(rad), b=math.sin(rad), c=-math.sin(rad), d=math.cos(rad))


def translation(x: float, y: float) -> Matrix:
    return Matrix(e=x, f=y)
