"""把 durable TaskRun terminal response 投影到现有 Channel Outbox。"""

from collections.abc import Callable, Mapping

from lobster0.automation.models import RunStatus, TaskResponse, TaskRun
from lobster0.automation.repository import TaskRunRepository
from lobster0.channels.delivery import split_message
from lobster0.storage.channels import DeliveryRepository, StoredDelivery


class TaskDeliveryService:
    """以 TaskRun 为幂等来源创建主动消息，不直接调用平台 SDK。"""

    def __init__(
        self,
        deliveries: DeliveryRepository,
        runs: TaskRunRepository,
        *,
        channel_max_chars: Mapping[str, int],
        wake: Callable[[], None] | None = None,
    ) -> None:
        """绑定 Outbox、各平台字符上限和可选 Worker 唤醒回调。"""
        limits = dict(channel_max_chars)
        if set(limits) != {"feishu", "telegram", "discord"} or any(
            type(value) is not int or value < 8 for value in limits.values()
        ):
            raise ValueError("task delivery channel limits are invalid")
        self._deliveries = deliveries
        self._runs = runs
        self._channel_max_chars = limits
        self._wake = wake

    def project(
        self,
        run: TaskRun,
        response: TaskResponse,
    ) -> tuple[StoredDelivery, ...]:
        """把已持久化 succeeded response 幂等分片，静默结果返回空 tuple。

        参数：
            run: 已结算且带冻结 snapshot/response 的 TaskRun。
            response: 调用方从同一 Run 读取的 terminal response。

        返回：
            已存在或新建的有序 Delivery tuple；静默/无路由时为空。

        异常：
            ValueError: Run 未成功、缺 snapshot，或 response 与落库值不一致。
        """
        if not isinstance(run, TaskRun) or run.status is not RunStatus.SUCCEEDED:
            raise ValueError("task delivery requires a succeeded run")
        if not isinstance(response, TaskResponse) or run.response != response:
            raise ValueError("task delivery response mismatch")
        if run.snapshot is None:
            raise ValueError("task delivery snapshot is missing")
        target = run.snapshot.delivery
        if not response.notify or target.route == "none":
            return ()
        channel = target.channel
        if channel not in self._channel_max_chars:
            raise ValueError("task delivery channel is unsupported")
        assert target.account_id is not None and target.conversation_id is not None
        parts = split_message(
            response.text,
            max_chars=self._channel_max_chars[channel],
            preserve_code_fences=channel == "telegram",
        )
        projected = self._deliveries.create_task_parts(
            task_run_id=run.id,
            channel=channel,
            account_id=target.account_id,
            external_conversation_id=target.conversation_id,
            reply_to_message_id="",
            kind="message",
            contents=parts,
        )
        if self._wake is not None:
            self._wake()
        return projected

    def recover(self) -> int:
        """重启时幂等补投影全部已成功 Run，返回实际可投递 Run 数。"""
        projected = 0
        for run in self._runs.list_succeeded():
            if run.response is None:
                continue
            deliveries = self.project(run, run.response)
            if deliveries:
                projected += 1
        return projected
