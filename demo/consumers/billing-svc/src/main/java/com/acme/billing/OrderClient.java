package com.acme.billing;

import java.math.BigDecimal;
import org.springframework.web.client.RestClient;

public class OrderClient {
    private final RestClient http = RestClient.create("http://order-service");

    public BigDecimal amountDue(String orderId) {
        OrderDto order = http.get().uri("/orders/" + orderId).retrieve().body(OrderDto.class);
        return order.total(); // a JSON number today
    }

    record OrderDto(String id, BigDecimal total, String currency) {}
}
