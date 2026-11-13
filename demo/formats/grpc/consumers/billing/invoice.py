import grpc

from shop.v1 import orders_pb2, orders_pb2_grpc


def amount_due(order_id: str) -> int:
    channel = grpc.insecure_channel("orders:50051")
    stub = orders_pb2_grpc.OrderServiceStub(channel)
    order = stub.GetOrder(orders_pb2.GetOrderRequest(id=order_id))
    return order.total.units
