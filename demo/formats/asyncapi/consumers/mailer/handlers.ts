import { Kafka } from "kafkajs";

const kafka = new Kafka({ brokers: ["kafka:9092"] });

export async function start() {
  const consumer = kafka.consumer({ groupId: "mailer" });
  await consumer.subscribe({ topic: "order.created" });
  await consumer.run({
    eachMessage: async ({ message }) => {
      const event = JSON.parse(message.value!.toString());
      await sendReceipt(event.customerEmail, event.total);
    },
  });
}

export async function requestCancel(orderId: string) {
  const producer = kafka.producer();
  await producer.send({ topic: "order.cancel", messages: [{ value: JSON.stringify({ orderId }) }] });
}

declare function sendReceipt(to: string, total: number): Promise<void>;
