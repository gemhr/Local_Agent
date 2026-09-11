#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""幂等创建 Compose 运行所需的 Kafka topics。"""

from __future__ import annotations

import os

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic


def main() -> None:
    bootstrap_servers = os.environ["LOCAL_AGENT_KAFKA_BOOTSTRAP_SERVERS"].strip()
    topic_names = (
        os.environ["LOCAL_AGENT_KAFKA_JOB_TOPIC"].strip(),
        os.environ["LOCAL_AGENT_KAFKA_JOB_DLQ_TOPIC"].strip(),
    )
    partitions = int(os.getenv("LOCAL_AGENT_KAFKA_TOPIC_PARTITIONS", "2"))
    if not bootstrap_servers or not all(topic_names) or partitions < 1:
        raise SystemExit("invalid Kafka topic provisioning configuration")

    client = AdminClient({"bootstrap.servers": bootstrap_servers})
    futures = client.create_topics(
        [
            NewTopic(name, num_partitions=partitions, replication_factor=1)
            for name in topic_names
        ]
    )
    for name, future in futures.items():
        try:
            future.result()
        except KafkaException as exc:
            error = exc.args[0] if exc.args else None
            if not isinstance(error, KafkaError) or error.code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise SystemExit(f"Kafka topic provisioning failed: {name}") from None


if __name__ == "__main__":
    main()
