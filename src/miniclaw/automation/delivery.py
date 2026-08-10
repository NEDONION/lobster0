"""把 durable TaskRun terminal response 投影到现有 Channel Outbox。"""

from collections.abc import Callable, Mapping

from miniclaw.automation.models import RunStatus, TaskResponse, TaskRun
from miniclaw.automation.repository import TaskRunRepository
from miniclaw.channels.approvals import (
    ApprovalPresentationRepository,
    approval_delivery_payload,
    approval_envelope,
)
from miniclaw.channels.delivery import split_message
from miniclaw.storage.channels import DeliveryRepository, StoredDelivery


class TaskDeliveryService:
    """以 TaskRun 为幂等来源创建主动消息，不直接调用平台 SDK。"""

    def __init__(
        self,
        deliveries: DeliveryRepository,
        runs: TaskRunRepository,
        *,
        approvals: ApprovalPresentationRepository | None = None,
        channel_max_chars: Mapping[str, int],
        wake: Callable[[], None] | None = None,
    ) -> None:
        """绑定 Outbox、审批展示源、平台字符上限和可选 Worker 唤醒回调。"""
        limits = dict(channel_max_chars)
        if set(limits) != {"feishu", "telegram", "discord"} or any(
            type(value) is not int or value < 8 for value in limits.values()
        ):
            raise ValueError("task delivery channel limits are invalid")
        self._deliveries = deliveries
        self._runs = runs
        self._approvals = approvals
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

    def project_approval(
        self,
        run: TaskRun,
        approval_id: int,
    ) -> tuple[StoredDelivery, ...]:
        """把 waiting Run 的参数绑定审批幂等投影到冻结的 Channel 目的地。

        参数：
            run: 已释放 lease 且绑定 Approval 的 waiting TaskRun。
            approval_id: 与 Run 持久化值一致的内部 Approval ID。

        返回：
            已存在或新建的单个 Approval Delivery；无投递路由时为空。

        异常：
            ValueError: Run 状态、审批绑定、快照、仓储或 Channel 不合法。
            ApprovalError: Core 审批不存在、过期、已结束或参数绑定失效。
        """
        if not isinstance(run, TaskRun) or run.status is not RunStatus.WAITING_APPROVAL:
            raise ValueError("task approval delivery requires a waiting run")
        if type(approval_id) is not int or run.approval_id != approval_id:
            raise ValueError("task approval delivery id mismatch")
        if run.snapshot is None:
            raise ValueError("task approval delivery snapshot is missing")
        target = run.snapshot.delivery
        if target.route == "none":
            return ()
        if self._approvals is None:
            raise ValueError("task approval delivery repository is missing")
        channel = target.channel
        if channel not in self._channel_max_chars:
            raise ValueError("task delivery channel is unsupported")
        assert target.account_id is not None and target.conversation_id is not None
        payload = approval_delivery_payload(
            approval_envelope(
                self._approvals.presentation(run.snapshot.owner_id, approval_id)
            )
        )
        projected = self._deliveries.create_task_parts(
            task_run_id=run.id,
            channel=channel,
            account_id=target.account_id,
            external_conversation_id=target.conversation_id,
            reply_to_message_id="",
            kind="approval",
            contents=(payload,),
        )
        if self._wake is not None:
            self._wake()
        return projected

    def recover(self) -> int:
        """重启时补投影成功回复和待审批卡片，返回实际可投递 Run 数。"""
        projected = 0
        for run in self._runs.list_succeeded():
            if run.response is None:
                continue
            deliveries = self.project(run, run.response)
            if deliveries:
                projected += 1
        if self._approvals is not None:
            for run in self._runs.list(
                statuses=(RunStatus.WAITING_APPROVAL,),
                limit=1000,
            ):
                if run.approval_id is None:
                    continue
                deliveries = self.project_approval(run, run.approval_id)
                if deliveries:
                    projected += 1
        return projected
