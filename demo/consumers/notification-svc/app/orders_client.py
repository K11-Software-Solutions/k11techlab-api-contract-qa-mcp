import httpx

ORDERS_URL = "http://order-service"


def order_shipped_email(order_id: str) -> dict:
    order = httpx.get(f"{ORDERS_URL}/orders/{order_id}").json()
    return {"to": order["customer"]["email"], "subject": f"Order {order['id']} is {order['status']}"}
