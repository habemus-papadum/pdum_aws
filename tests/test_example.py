"""Example tests for aws."""

from pdum import aws


def test_version():
    """Test that the package has a version."""
    assert hasattr(aws, "__version__")
    assert isinstance(aws.__version__, str)
    assert len(aws.__version__) > 0


def test_import():
    """Test that the package can be imported."""
    assert aws is not None

