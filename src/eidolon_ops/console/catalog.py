"""Every operation the console can ask a Host for, declared once.

One table, the same way ``plans`` is one table. An entry says which capability
a Host must hold to be offered the operation, what it needs to be told, which
of those inputs is the one that turns a rehearsal into a mutation, how to build
the plan, and how to invoke it. Nothing about an operation is spelled in the
API layer or in the browser: the console offers what the Host says it can do,
and asks for confirmation proportional to what the plan says it touches.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from eidolon_ops import plans
from eidolon_ops.console.errors import ConsoleError
from eidolon_ops.host_controller import HostController
from eidolon_ops.model import Capability, DestructiveLevel, Evidence, Plan

__all__ = [
    "CATALOG",
    "Confirmation",
    "Field",
    "Operation",
    "coerce_parameters",
    "operation",
    "required_confirmation",
]


class Confirmation(StrEnum):
    """How much the operator has to say before the console will act."""

    #: Read-only, or a rehearsal that changes nothing.
    NONE = "none"
    #: Mutating and walkable-back: one explicit acknowledgement.
    ACKNOWLEDGE = "acknowledge"
    #: Irreversible: the Host's own id has to be typed out.
    TYPED_HOST_ID = "typed-host-id"


class Kind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    #: An absolute path on this workstation. Relative paths are refused rather
    #: than resolved: a server process's working directory is not a place an
    #: operator can see, so a backup landing there would be a backup lost.
    PATH = "path"


@dataclass(frozen=True, slots=True)
class Field:
    """One input an operation needs, and what makes it valid."""

    name: str
    kind: Kind
    label: str
    help: str = ""
    required: bool = False
    default: object = None
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    #: Turning this on is what makes the operation mutate. Rendered apart from
    #: the ordinary inputs, because it is the one the confirmation is about.
    gate: bool = False
    #: Offered only on a Host that holds this capability.
    capability: Capability | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    """One thing an operator can ask of one Host."""

    name: str
    label: str
    summary: str
    capability: Capability
    group: str
    #: Builds the plan from validated parameters. Pure: no Host is contacted,
    #: which is what lets the console show the gradient before asking.
    plan: Callable[[str, Mapping[str, Any]], Plan] = field(repr=False)
    invoke: Callable[[HostController, Mapping[str, Any]], Evidence] = field(repr=False)
    fields: tuple[Field, ...] = ()
    #: Whether these parameters mean "do it" rather than "show me what you would
    #: do". A plan's ``requires_flags`` cannot answer this alone: ``reset`` names
    #: ``--wipe-authority-data`` among its flags while still only planning.
    applies: Callable[[Mapping[str, Any]], bool] = field(
        repr=False, default=lambda _params: True
    )
    #: Set where the operation's own report carries a value that exists to be
    #: read once by a human and never stored.
    sensitive_keys: tuple[str, ...] = ()


def _release_id() -> Field:
    return Field(
        name="release_id",
        kind=Kind.STRING,
        label="Release ID",
        help="发布矩阵的标识，例如 20260807-product-1",
        required=True,
    )


def _apply(label: str, help_text: str) -> Field:
    return Field(name="apply", kind=Kind.BOOLEAN, label=label, help=help_text, gate=True)


CATALOG: tuple[Operation, ...] = (
    Operation(
        name="status",
        label="状态",
        summary="读取 unit、release 与 receipt 状态",
        capability=Capability.STATUS,
        group="observe",
        plan=lambda host_id, _params: plans.status(host_id),
        invoke=lambda controller, _params: controller.status(),
    ),
    Operation(
        name="doctor",
        label="体检",
        summary="路径契约、capability 集合、固定基础环境与 Host 自报健康",
        capability=Capability.DOCTOR,
        group="observe",
        fields=(
            Field(
                name="release_id",
                kind=Kind.STRING,
                label="Release ID",
                help="留空则体检当前激活的 release",
            ),
        ),
        plan=lambda host_id, _params: plans.doctor(host_id),
        invoke=lambda controller, params: controller.doctor(release_id=params["release_id"]),
    ),
    Operation(
        name="app-ready",
        label="App 就绪门禁",
        summary="逐条作证手机 App 可管理这台 Host 所需的事实",
        capability=Capability.APP_READY,
        group="observe",
        plan=lambda host_id, _params: plans.app_ready(host_id),
        invoke=lambda controller, _params: controller.app_ready(),
    ),
    Operation(
        name="logs",
        label="日志",
        summary="读取有界的日志输出",
        capability=Capability.LOGS,
        group="observe",
        fields=(
            Field(
                name="service",
                kind=Kind.STRING,
                label="服务",
                help="留空读取全部；组件名与 unit 名都接受",
            ),
            Field(
                name="lines",
                kind=Kind.INTEGER,
                label="行数",
                default=200,
                minimum=1,
                maximum=5000,
            ),
            Field(
                name="since",
                kind=Kind.STRING,
                label="起始时间",
                help="journal 表达式，例如 -30min 或 2026-08-18 09:00",
                capability=Capability.LOG_HISTORY,
            ),
        ),
        plan=lambda host_id, _params: plans.logs(host_id),
        invoke=lambda controller, params: controller.logs(
            service=params["service"], lines=params["lines"], since=params["since"]
        ),
    ),
    Operation(
        name="start",
        label="启动",
        summary="把产品作为一个边界动作启动",
        capability=Capability.LIFECYCLE,
        group="lifecycle",
        fields=(
            Field(
                name="dry_run",
                kind=Kind.BOOLEAN,
                label="仅演练",
                help="只打印将要执行的命令，不动这台 Host",
            ),
        ),
        plan=lambda host_id, params: plans.lifecycle(host_id, "start", dry_run=params["dry_run"]),
        invoke=lambda controller, params: controller.lifecycle(
            "start", dry_run=params["dry_run"]
        ),
        applies=lambda params: not params["dry_run"],
    ),
    Operation(
        name="stop",
        label="停止",
        summary="把产品作为一个边界动作停止",
        capability=Capability.LIFECYCLE,
        group="lifecycle",
        fields=(
            Field(name="dry_run", kind=Kind.BOOLEAN, label="仅演练"),
        ),
        plan=lambda host_id, params: plans.lifecycle(host_id, "stop", dry_run=params["dry_run"]),
        invoke=lambda controller, params: controller.lifecycle("stop", dry_run=params["dry_run"]),
        applies=lambda params: not params["dry_run"],
    ),
    Operation(
        name="restart",
        label="重启",
        summary="把产品作为一个边界动作重启",
        capability=Capability.LIFECYCLE,
        group="lifecycle",
        fields=(
            Field(name="dry_run", kind=Kind.BOOLEAN, label="仅演练"),
        ),
        plan=lambda host_id, params: plans.lifecycle(
            host_id, "restart", dry_run=params["dry_run"]
        ),
        invoke=lambda controller, params: controller.lifecycle(
            "restart", dry_run=params["dry_run"]
        ),
        applies=lambda params: not params["dry_run"],
    ),
    Operation(
        name="commissioning-code",
        label="Setup 码",
        summary="签发一枚有寿命上限的一次性 Setup 码",
        capability=Capability.COMMISSIONING_CODE,
        group="lifecycle",
        fields=(
            Field(
                name="ttl_seconds",
                kind=Kind.INTEGER,
                label="有效期（秒）",
                default=600,
                minimum=60,
                maximum=86400,
            ),
        ),
        plan=lambda host_id, _params: plans.commissioning_code(host_id),
        invoke=lambda controller, params: controller.commissioning_code(
            ttl_seconds=params["ttl_seconds"]
        ),
        sensitive_keys=("setup_code",),
    ),
    Operation(
        name="provision",
        label="基础环境",
        summary="检测，并在放行后安装固定版本的非 Eidolon 基础环境",
        capability=Capability.PROVISION,
        group="release",
        fields=(_apply("执行安装", "不勾选只做只读检测"),),
        plan=lambda host_id, params: plans.provision(host_id, apply=params["apply"]),
        invoke=lambda controller, params: controller.provision(apply=params["apply"]),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="init-inputs",
        label="私密输入集",
        summary="在这台工作站上创建 14 个 mode-0600 首装输入文件，永不覆盖",
        capability=Capability.INIT_INPUTS,
        group="secret",
        fields=(
            Field(
                name="new_identity",
                kind=Kind.BOOLEAN,
                label="换发 Host 身份",
                help="退役这台机器现有的 Host identity 并另发一枚；这会改变 ehost-* 与 .local 名称",
            ),
        ),
        plan=lambda host_id, params: plans.initialize_inputs(
            host_id, new_identity=params["new_identity"]
        ),
        invoke=lambda controller, params: controller.initialize_inputs(
            new_identity=params["new_identity"]
        ),
    ),
    Operation(
        name="install",
        label="首次安装",
        summary="封印 bundle、原生构建、创建身份与 Data 基线、装齐产品 unit",
        capability=Capability.INSTALL,
        group="release",
        fields=(
            _release_id(),
            Field(name="resume", kind=Kind.BOOLEAN, label="续传", help="复用已校验的 bundle"),
            Field(
                name="reset_existing",
                kind=Kind.BOOLEAN,
                label="先清除既有部署",
                help="install --apply 只接受全新 namespace；重装既有 Host 必须同时勾选下一项",
            ),
            Field(
                name="wipe_authority_data",
                kind=Kind.BOOLEAN,
                label="永久删除权威数据",
                help="删除 Eidolon 与 Bootstrap 的全部权威数据，不可逆",
            ),
            _apply("执行安装", "不勾选只列出将要发生的每一处变更"),
        ),
        plan=lambda host_id, params: plans.install(
            host_id,
            apply=params["apply"],
            reset_existing=params["reset_existing"],
            wipe_authority_data=params["wipe_authority_data"],
        ),
        invoke=lambda controller, params: controller.install(
            release_id=params["release_id"],
            resume=params["resume"],
            apply=params["apply"],
            reset_existing=params["reset_existing"],
            wipe_authority_data=params["wipe_authority_data"],
        ),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="deploy",
        label="发布/更新",
        summary="封印、上传、原生构建、演练切换，放行后原子激活并通过健康门禁",
        capability=Capability.DEPLOY,
        group="release",
        fields=(
            _release_id(),
            Field(name="resume", kind=Kind.BOOLEAN, label="续传"),
            Field(
                name="cutover_mode",
                kind=Kind.STRING,
                label="切换模式",
                help="reversible 可恢复完整 previous state；forward-only 跨持久化屏障后只允许同 schema 修复",
                default="reversible",
                choices=("reversible", "forward-only"),
            ),
            Field(
                name="activate",
                kind=Kind.BOOLEAN,
                label="执行切换",
                help="不勾选只做到 dry-run；门禁不过会自动恢复原快照",
                gate=True,
            ),
        ),
        plan=lambda host_id, params: plans.deploy(host_id, activate=params["activate"]),
        invoke=lambda controller, params: controller.deploy(
            release_id=params["release_id"],
            resume=params["resume"],
            activate=params["activate"],
            cutover_mode=params["cutover_mode"],
        ),
        applies=lambda params: params["activate"],
    ),
    Operation(
        name="rollback",
        label="回滚",
        summary="恢复一份精确的 release 快照",
        capability=Capability.ROLLBACK,
        group="release",
        fields=(
            _release_id(),
            Field(
                name="snapshot",
                kind=Kind.PATH,
                label="快照路径",
                help="必须是 deployment_evidence 的直接子目录",
                required=True,
            ),
            _apply("执行恢复", "不勾选只取回滚计划"),
        ),
        plan=lambda host_id, params: plans.rollback(host_id, apply=params["apply"]),
        invoke=lambda controller, params: controller.rollback(
            release_id=params["release_id"],
            snapshot=params["snapshot"],
            apply=params["apply"],
        ),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="backup",
        label="备份",
        summary="快照每一个声明了备份方式的权威，并取回这台工作站",
        capability=Capability.BACKUP,
        group="authority",
        fields=(
            Field(
                name="output",
                kind=Kind.PATH,
                label="输出文件",
                help="这台工作站上的绝对路径",
                required=True,
            ),
        ),
        plan=lambda host_id, _params: plans.backup(host_id),
        invoke=lambda controller, params: controller.backup(output=params["output"]),
    ),
    Operation(
        name="restore",
        label="恢复",
        summary="把备份放回它来自的那台 Host；产品会为此停机",
        capability=Capability.RESTORE,
        group="authority",
        fields=(
            Field(
                name="source",
                kind=Kind.PATH,
                label="备份文件",
                help="这台工作站上的绝对路径",
                required=True,
            ),
            _apply("执行恢复", "不勾选只取恢复计划"),
        ),
        plan=lambda host_id, params: plans.restore(host_id, apply=params["apply"]),
        invoke=lambda controller, params: controller.restore(
            source=params["source"], apply=params["apply"]
        ),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="authority-backup",
        label="备份 Owner Authority",
        summary="封存 Owner root、完整 Hub 状态与同代 lineage 供显式恢复",
        capability=Capability.BACKUP,
        group="authority",
        fields=(
            Field(
                name="output",
                kind=Kind.PATH,
                label="输出目录",
                help="这台工作站上的绝对目录",
                required=True,
            ),
        ),
        plan=lambda host_id, _params: plans.authority_backup(host_id),
        invoke=lambda controller, params: controller.authority_backup(output=params["output"]),
    ),
    Operation(
        name="authority-restore",
        label="恢复 Owner Authority",
        summary="从完整备份恢复同一 generation；不会 Reset、commission 或重新 Claim",
        capability=Capability.RESTORE,
        group="authority",
        fields=(
            Field(
                name="source",
                kind=Kind.PATH,
                label="恢复包",
                help="authority-restore.json 所在的绝对目录",
                required=True,
            ),
            _apply("执行恢复", "不勾选只验证完整性与恢复计划"),
        ),
        plan=lambda host_id, params: plans.authority_restore(host_id, apply=params["apply"]),
        invoke=lambda controller, params: controller.authority_restore(
            source=params["source"], apply=params["apply"]
        ),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="reset",
        label="清除部署",
        summary="停用并移除固定的 Eidolon 部署 namespace",
        capability=Capability.RESET,
        group="authority",
        fields=(
            Field(
                name="wipe_authority_data",
                kind=Kind.BOOLEAN,
                label="连权威数据一起删",
                help="不勾选则保留 /var/lib；勾选后删除的数据无法恢复",
            ),
            _apply("执行清除", "不勾选只列出会被删除的确切范围"),
        ),
        plan=lambda host_id, params: plans.reset(
            host_id, apply=params["apply"], wipe_authority_data=params["wipe_authority_data"]
        ),
        invoke=lambda controller, params: controller.reset(
            wipe_authority_data=params["wipe_authority_data"], apply=params["apply"]
        ),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="kernel-schema-reset",
        label="挪开旧 Kernel 权威",
        summary="把这台 Host 上 Kernel 自己打不开的那份权威改名挪开，Kernel 才能起来",
        capability=Capability.KERNEL_SCHEMA_RESET,
        group="authority",
        fields=(
            Field(
                name="forget_selections",
                kind=Kind.INTEGER,
                label="确认会丢掉的 Owner 选择条数",
                help=(
                    "先不勾执行、看计划里报出的条数，再原样填回来。"
                    "只有这一个数字是这台 Host 上没有任何副本能还原的"
                ),
                default=0,
                minimum=0,
            ),
            Field(
                name="forget_uncounted_selections",
                kind=Kind.BOOLEAN,
                label="这份库数不出来，我认了",
                help=(
                    "只有当计划说 Kernel 认不出这份库的形状时才勾。"
                    "数得出来的时候勾它会被拒绝——那时候要填上面那个数字"
                ),
            ),
            _apply("执行改名", "不勾选只问 Kernel 认不认这份库、以及挪开会丢什么"),
        ),
        plan=lambda host_id, params: plans.kernel_schema_reset(host_id, apply=params["apply"]),
        invoke=lambda controller, params: controller.kernel_schema_reset(
            apply=params["apply"],
            forget_selections=params["forget_selections"],
            forget_uncounted_selections=params["forget_uncounted_selections"],
        ),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="controller-reset",
        label="吊销控制权",
        summary="吊销这台 Host 上的每一份 Controller Grant，让新手机可以认领",
        capability=Capability.CONTROLLER_RESET,
        group="authority",
        fields=(_apply("执行吊销", "不勾选只取计划"),),
        plan=lambda host_id, params: plans.controller_reset(host_id, apply=params["apply"]),
        invoke=lambda controller, params: controller.controller_reset(apply=params["apply"]),
        applies=lambda params: params["apply"],
    ),
    Operation(
        name="diagnose",
        label="诊断包",
        summary="收集一份已脱敏的诊断归档",
        capability=Capability.DIAGNOSE,
        group="observe",
        fields=(
            Field(
                name="output",
                kind=Kind.PATH,
                label="输出文件",
                help="这台工作站上的绝对路径，以 .tar.gz 结尾",
                required=True,
            ),
        ),
        plan=lambda host_id, _params: plans.diagnose(host_id),
        invoke=lambda controller, params: controller.diagnose(output=params["output"]),
    ),
    Operation(
        name="debug",
        label="源码运行诊断",
        summary="macOS source-run 的实现级诊断，不是产品操作",
        capability=Capability.SOURCE_PROFILE,
        group="observe",
        fields=(
            Field(
                name="profile_operation",
                kind=Kind.STRING,
                label="子操作",
                required=True,
                default="status",
                choices=(
                    "prepare",
                    "validate",
                    "status",
                    "web-start",
                    "web-stop",
                    "web-restart",
                    "web-status",
                    "commissioning-code",
                ),
            ),
        ),
        plan=lambda host_id, params: plans.source_profile(host_id, params["profile_operation"]),
        invoke=lambda controller, params: controller.local_profile(
            "product-source", params["profile_operation"]
        ),
        sensitive_keys=("setup_code",),
    ),
)

_BY_NAME = {item.name: item for item in CATALOG}


def operation(name: str) -> Operation:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ConsoleError(f"no such operation: {name}", status=404) from None


def coerce_parameters(
    spec: Operation,
    raw: Mapping[str, object],
    *,
    capabilities: frozenset[Capability],
) -> dict[str, Any]:
    """Validate what the browser sent, and fill in what it left out.

    Every field the operation declares is present in the result, so the invoke
    and plan callables never guess at a missing key. A field the browser sent
    that the operation never declared is refused rather than ignored: it is
    either a stale client or an operator being told the wrong thing about what
    is going to happen.
    """

    unknown = sorted(set(raw) - {item.name for item in spec.fields})
    if unknown:
        raise ConsoleError(f"{spec.name} takes no such parameter: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for item in spec.fields:
        if item.capability is not None and item.capability not in capabilities:
            if raw.get(item.name) not in (None, "", False):
                raise ConsoleError(
                    f"{item.name} needs the {item.capability} capability, "
                    "which this Host does not hold"
                )
            values[item.name] = _empty(item)
            continue
        values[item.name] = _coerce(spec, item, raw.get(item.name))
    return values


def _empty(item: Field) -> Any:
    return False if item.kind is Kind.BOOLEAN else None


def _coerce(spec: Operation, item: Field, value: object) -> Any:
    if value is None or value == "":
        if item.required:
            raise ConsoleError(f"{spec.name} requires {item.name}")
        return item.default if item.kind is not Kind.BOOLEAN else bool(item.default)
    if item.kind is Kind.BOOLEAN:
        if not isinstance(value, bool):
            raise ConsoleError(f"{item.name} must be a boolean")
        return value
    if item.kind is Kind.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConsoleError(f"{item.name} must be an integer")
        if item.minimum is not None and value < item.minimum:
            raise ConsoleError(f"{item.name} must be at least {item.minimum}")
        if item.maximum is not None and value > item.maximum:
            raise ConsoleError(f"{item.name} must be at most {item.maximum}")
        return value
    if not isinstance(value, str):
        raise ConsoleError(f"{item.name} must be a string")
    text = value.strip()
    if item.choices and text not in item.choices:
        raise ConsoleError(f"{item.name} must be one of: {', '.join(item.choices)}")
    if item.kind is Kind.PATH:
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ConsoleError(f"{item.name} must be an absolute path")
        return path
    return text


def required_confirmation(spec: Operation, params: Mapping[str, Any]) -> Confirmation:
    """How much confirmation these exact parameters demand.

    Read off the plan rather than off the operation's name: ``install`` without
    ``--apply`` is a rehearsal and asks for nothing, while the same operation
    with ``--wipe-authority-data`` asks for the Host's id in full.
    """

    if not spec.applies(params):
        return Confirmation.NONE
    plan = spec.plan("preview", params)
    if plan.destructive is DestructiveLevel.IRREVERSIBLE:
        return Confirmation.TYPED_HOST_ID
    if plan.touches:
        return Confirmation.ACKNOWLEDGE
    return Confirmation.NONE


def available(capabilities: frozenset[Capability]) -> Sequence[Operation]:
    """The operations this Host says it can perform, in catalog order."""

    return tuple(item for item in CATALOG if item.capability in capabilities)
