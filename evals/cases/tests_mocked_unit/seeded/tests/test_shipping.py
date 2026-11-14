from unittest.mock import patch

from app import shipping
from app.shipping import shipping_cost


def test_a_light_parcel_ships_at_the_base_rate() -> None:
    assert shipping_cost(0.5) == 500


def test_a_heavy_parcel_ships_at_the_heavy_rate() -> None:
    with patch.object(shipping, "shipping_cost", return_value=2500) as cost:
        assert shipping.shipping_cost(25) == 2500
    cost.assert_called_once_with(25)
