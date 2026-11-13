from zeep import Client

client = Client("http://orders.internal/orders?wsdl")


def order_total(order_id: str) -> str:
    result = client.service.GetOrder(OrderId=order_id)
    return result.Order.Total
