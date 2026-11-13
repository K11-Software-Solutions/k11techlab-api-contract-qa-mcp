import requests


def export_orders(base):
    return [(o["id"], o["status"]) for o in requests.get(base + "/orders", params={"status": "paid"}).json()]
