import pytest
from math_utils import triangular_number

def test_zero():
    assert triangular_number(0) == 0

def test_negative():
    with pytest.raises(ValueError):
        triangular_number(-1)

def test_triangular_small():
    assert triangular_number(1) == 1
    assert triangular_number(2) == 3
    assert triangular_number(3) == 6
    assert triangular_number(4) == 10
