import pytest

from rest_framework.serializers import ValidationError

import pulpcore.app.tasks.importer
from pulpcore.app.tasks.importer import _check_versions


class TestObject:
    version = "1.2.3"  # Every component is vers 1.2.3


def test_vers_check(monkeypatch):
    monkeypatch.setattr(pulpcore.app.tasks.importer, "get_distribution", lambda _: TestObject())
    export_json = [{"component": "xyz", "version": "1.2.3"}]
    _check_versions(export_json)

    export_json = [{"component": "xy", "version": "1.2"}]
    _check_versions(export_json)

    export_json = [{"component": "x_noty_z", "version": "1.4.3"}]
    with pytest.raises(ValidationError):
        _check_versions(export_json)

    export_json = [{"component": "notx_y_z", "version": "2.2.3"}]
    with pytest.raises(ValidationError):
        _check_versions(export_json)
