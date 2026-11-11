"""Development reproductions derived solely from public issue #1357."""

from datetime import datetime

import pytest
from marshmallow import Schema, fields


@pytest.mark.parametrize("container", ["list", "tuple"])
def test_datetime_inside_container_initializes_and_round_trips(container):
    inner = fields.DateTime()
    field = fields.List(inner) if container == "list" else fields.Tuple((inner,))
    schema = type("IssueSchema", (Schema,), {"times": field})()
    value = datetime(2019, 8, 21, 12, 34, 56)
    original = [value] if container == "list" else (value,)
    serialized = schema.dump({"times": original})
    assert list(serialized["times"]) == ["2019-08-21T12:34:56"]
    assert list(schema.load(serialized)["times"]) == [value]
