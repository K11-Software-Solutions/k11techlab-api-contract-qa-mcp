import { gql } from "@apollo/client";

export const ORDER_SUMMARY = gql`
  query OrderSummary($id: ID!) {
    order(id: $id) {
      id
      status
      ...Money
    }
  }
  fragment Money on Order {
    total
    currency
  }
`;
