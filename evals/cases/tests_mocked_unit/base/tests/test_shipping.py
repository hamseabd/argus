from app.shipping import shipping_cost


def test_a_light_parcel_ships_at_the_base_rate() -> None:
    assert shipping_cost(0.5) == 500
