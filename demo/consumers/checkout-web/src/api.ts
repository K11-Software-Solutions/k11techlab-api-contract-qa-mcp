export async function placeOrder(customerId: string, items: { sku: string; qty: number }[]) {
  const res = await fetch(`/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ customerId, items }),
  });
  return res.json();
}

export async function trackOrder(id: string) {
  return (await fetch(`/orders/${id}/tracking`)).json();
}
