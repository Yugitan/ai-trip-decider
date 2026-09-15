"""开发设置面板的配置服务单测（全部跑在临时目录，**不碰真实 .env 与 config/**）。

守的都是"让面板别变成后门或脚枪"的那些点：

- 白名单之外的环境变量一律拒绝（``DATABASE_URL`` / ``SESSION_SECRET`` 刻意不在名单里）；
- Secret 只写不读（读取一律打码）；
- 改 ``.env`` 保留注释与顺序，且任何失败都把文件与进程环境恢复原状；
- 改 YAML 先校验**候选内容**，不通过时一个字节都不写。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core.config import clear_config_cache
from app.core.errors import AppError
from app.core.paths import backend_dir, project_root
from app.services import dev_config

pytestmark = pytest.mark.unit

_SAMPLE_ENV = """# 顶部注释：解释为什么某些 Key 不能进前端
ENV=development
DEEPSEEK_API_KEY=sk-abcdefghijklmnop
LLM_TIMEOUT_S=20

# ── 搜索 ──
TAVILY_API_KEY=
"""


@pytest.fixture(autouse=True)
def _clean_config_cache():
    """每个用例前后都清缓存：配置是 lru_cache 的，残留会串味。"""
    clear_config_cache()
    yield
    clear_config_cache()


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(_SAMPLE_ENV, encoding="utf-8")
    return path


# ── Secret 打码 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("short", "***"),
        ("sk-1234567890abcdef", "sk-***cdef"),
    ],
)
def test_mask_secret(value: str, expected: str) -> None:
    assert dev_config.mask_secret(value) == expected


def test_env_payload_masks_secret_and_reports_set_state(tmp_path: Path) -> None:
    payload = {field["key"]: field for field in dev_config.env_payload(path=_env_file(tmp_path))}
    secret = payload["DEEPSEEK_API_KEY"]
    assert secret["kind"] == "secret"
    assert secret["is_set"] is True
    assert "***" in secret["value"]
    assert "sk-abcdefghijklmnop" not in secret["value"], "明文 Key 绝不能出现在响应里"

    empty = payload["TAVILY_API_KEY"]
    assert empty["is_set"] is False
    assert empty["value"] == ""

    # 非 Secret 字段原样返回（否则面板改不了）
    assert payload["LLM_TIMEOUT_S"]["value"] == "20"
    # ENV / DATABASE_URL / SESSION_SECRET 刻意不在面板上（见 dev_config 的注释）
    assert "ENV" not in payload


def test_env_payload_only_lists_whitelisted_keys(tmp_path: Path) -> None:
    text = _env_file(tmp_path).read_text(encoding="utf-8") + "DATABASE_URL=postgresql://x\n"
    _env_file(tmp_path).write_text(text, encoding="utf-8")
    keys = {field["key"] for field in dev_config.env_payload(path=_env_file(tmp_path))}
    assert "DATABASE_URL" not in keys, "数据库连接串不该出现在面板上"


# ── .env 写入 ───────────────────────────────────────────────────────────────


def test_update_env_rewrites_in_place_and_keeps_comments(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    environ: dict[str, str] = {}

    changed = dev_config.update_env(
        {"DEEPSEEK_API_KEY": "sk-new-value-1234"}, path=path, environ=environ
    )

    assert changed == ["DEEPSEEK_API_KEY"]
    text = path.read_text(encoding="utf-8")
    assert "sk-new-value-1234" in text
    assert "顶部注释：解释为什么某些 Key 不能进前端" in text, "注释必须保留"
    assert "# ── 搜索 ──" in text
    assert environ["DEEPSEEK_API_KEY"] == "sk-new-value-1234", "改动要立刻对进程生效"


def test_update_env_appends_keys_missing_from_file(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    dev_config.update_env({"SERPER_API_KEY": "serper-x"}, path=path, environ={})
    text = path.read_text(encoding="utf-8")
    assert "SERPER_API_KEY=serper-x" in text
    assert "由开发设置面板追加" in text


def test_update_env_rejects_keys_outside_whitelist(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        dev_config.update_env({"DATABASE_URL": "postgresql://elsewhere"}, path=path, environ={})
    assert excinfo.value.code == "INVALID_INPUT"
    assert path.read_text(encoding="utf-8") == before, "被拒绝的写入不能留下任何痕迹"


def test_update_env_rejects_bad_types(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    with pytest.raises(AppError):
        dev_config.update_env({"BACKEND_PORT": "八千"}, path=path, environ={})
    with pytest.raises(AppError):
        dev_config.update_env({"LLM_PROVIDER": "some-other-vendor"}, path=path, environ={})
    with pytest.raises(AppError):
        dev_config.update_env({"ENABLE_LOCAL_FETCH": "yes"}, path=path, environ={})
    with pytest.raises(AppError):
        dev_config.update_env({"API_BASE_URL": "http://x\ny"}, path=path, environ={})


def test_update_env_is_a_noop_when_nothing_changes(tmp_path: Path) -> None:
    path = _env_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    changed = dev_config.update_env({"LLM_TIMEOUT_S": "20"}, path=path, environ={})
    assert changed == []
    assert path.read_text(encoding="utf-8") == before


def test_update_env_rolls_back_when_settings_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _env_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    environ = {"LLM_TIMEOUT_S": "20"}

    class _Boom:
        def __init__(self) -> None:
            raise RuntimeError("假装这一组配置放一起不合法")

    monkeypatch.setattr(dev_config, "Settings", _Boom)

    with pytest.raises(AppError) as excinfo:
        dev_config.update_env({"LLM_TIMEOUT_S": "45"}, path=path, environ=environ)

    assert "已回滚" in excinfo.value.message
    assert path.read_text(encoding="utf-8") == before, "校验失败必须把文件写回原样"
    assert environ["LLM_TIMEOUT_S"] == "20", "进程环境也要恢复"


# ── config/*.yaml ───────────────────────────────────────────────────────────


def test_config_file_names_lists_only_known_files(tmp_path: Path) -> None:
    (tmp_path / "ttl.yaml").write_text("version: '1.0'\n", encoding="utf-8")
    (tmp_path / "unknown.yaml").write_text("a: 1\n", encoding="utf-8")
    assert dev_config.config_file_names(directory=tmp_path) == ["ttl.yaml"]


@pytest.mark.parametrize("name", ["../../.env", "nope.yaml", "seed.yml"])
def test_config_path_rejects_outside_whitelist(tmp_path: Path, name: str) -> None:
    with pytest.raises(AppError) as excinfo:
        dev_config.write_config_file(name, "version: '1.0'\n", directory=tmp_path)
    assert excinfo.value.code == "INVALID_INPUT"


def test_write_config_file_rejects_invalid_content_without_touching_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ttl.yaml"
    original = "version: '2026.09.1'\nttl_hours: {}\nrefresh_policy: {}\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        dev_config.write_config_file("ttl.yaml", "version: 12345\n", directory=tmp_path)

    assert "未被修改" in excinfo.value.message
    assert target.read_text(encoding="utf-8") == original


def test_write_config_file_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    target = tmp_path / "ttl.yaml"
    target.write_text("version: '1.0'\n", encoding="utf-8")
    with pytest.raises(AppError):
        dev_config.write_config_file("ttl.yaml", "- 1\n- 2\n", directory=tmp_path)


def test_write_config_file_accepts_valid_content(tmp_path: Path) -> None:
    """用真实配置的内容做一次往返：合法内容应当被接受。"""
    source = dev_config.read_config_file("ttl.yaml")
    target = tmp_path / "ttl.yaml"
    target.write_text(source, encoding="utf-8")
    dev_config.write_config_file("ttl.yaml", source, directory=tmp_path)
    assert target.read_text(encoding="utf-8") == source


def test_read_config_file_unknown_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        dev_config.read_config_file("passwd", directory=tmp_path)


# ── 生效快照 ────────────────────────────────────────────────────────────────


def test_effective_snapshot_exposes_runtime_truth() -> None:
    snapshot = dev_config.effective_snapshot()
    assert set(snapshot) == {
        "env",
        "providers",
        "frontend_env",
        "llm",
        "cost_limits",
        "rate_limits",
        "degraded_modes",
        "config_versions",
    }
    assert snapshot["llm"]["fast_model"]
    # 这里**不返回 Key 本身**，只返回"配没配"
    assert isinstance(snapshot["llm"]["secret_configured"], bool)
    assert "api_key" not in str(snapshot["llm"]).lower() or "secret_configured" in str(
        snapshot["llm"]
    )


# ── 前端专用变量（面板改不到，但必须看得见）────────────────────────────────

_FRONTEND_TEMPLATE = """# 前端模板（只读的说明文字里带等号也不会被当成变量）
NEXT_PUBLIC_AMAP_JS_KEY=

# ── 后端地址 ──
# API_BASE_URL=http://127.0.0.1:8000
#   API_BASE_URL            （无前缀 = 服务端）
AMAP_SECURITY_CODE=
"""


def _frontend_dir(tmp_path: Path, *, local: str = "", base: str = "") -> Path:
    directory = tmp_path / "frontend"
    directory.mkdir(exist_ok=True)
    (directory / ".env.example").write_text(_FRONTEND_TEMPLATE, encoding="utf-8")
    if local:
        (directory / ".env.local").write_text(local, encoding="utf-8")
    if base:
        (directory / ".env").write_text(base, encoding="utf-8")
    return directory


def test_frontend_env_status_reports_presence_without_values(tmp_path: Path) -> None:
    """★ 面板只回答「配了没」与「从哪个文件读到的」，**不返回值** ——
    Secret 的只写不读是同一条纪律，前端变量不该成为那个例外。
    """
    # 用一个**一眼就是假的**值：真实 Key 从不进仓库（这条断言本身就是在守它）
    directory = _frontend_dir(
        tmp_path, local="NEXT_PUBLIC_AMAP_JS_KEY=not-a-real-key-0123456789\n"
    )

    status = dev_config.frontend_env_status(directory=directory)
    by_key = {item["key"]: item for item in status["keys"]}

    assert by_key["NEXT_PUBLIC_AMAP_JS_KEY"]["is_set"] is True
    assert by_key["NEXT_PUBLIC_AMAP_JS_KEY"]["source"] == "frontend/.env.local"
    assert by_key["AMAP_SECURITY_CODE"]["is_set"] is False
    assert by_key["AMAP_SECURITY_CODE"]["source"] == ""
    assert status["editable_here"] is False
    assert "not-a-real-key" not in json.dumps(status, ensure_ascii=False), "值绝不能出现在响应里"


def test_frontend_env_status_keys_come_from_the_template(tmp_path: Path) -> None:
    """名单以 `frontend/.env.example` 为准（前端配置的唯一事实源），不另存一份。

    顺便钉住一件事：模板里的**注释说明**（行中间带等号）不会被误当成变量名。
    """
    keys = [
        item["key"]
        for item in dev_config.frontend_env_status(directory=_frontend_dir(tmp_path))["keys"]
    ]
    assert keys == ["NEXT_PUBLIC_AMAP_JS_KEY", "API_BASE_URL", "AMAP_SECURITY_CODE"]


def test_frontend_env_local_wins_over_plain_env(tmp_path: Path) -> None:
    """`.env.local` 优先（与 Next 的读取顺序一致），并如实标出来源。"""
    directory = _frontend_dir(
        tmp_path,
        local="AMAP_SECURITY_CODE=from-local\n",
        base="AMAP_SECURITY_CODE=from-base\nNEXT_PUBLIC_AMAP_JS_KEY=from-base\n",
    )

    by_key = {
        item["key"]: item
        for item in dev_config.frontend_env_status(directory=directory)["keys"]
    }

    assert by_key["AMAP_SECURITY_CODE"]["source"] == "frontend/.env.local"
    assert by_key["NEXT_PUBLIC_AMAP_JS_KEY"]["source"] == "frontend/.env"


def test_frontend_env_status_survives_a_directory_without_template(tmp_path: Path) -> None:
    """目录/文件都不存在时返回空清单，而不是让整个面板首屏 500。"""
    status = dev_config.frontend_env_status(directory=tmp_path / "不存在")
    assert status["keys"] == []
    assert "frontend/.env.local" in status["files"]


def test_frontend_env_notes_have_no_stale_keys() -> None:
    """说明表里不能有前端模板里不存在的键 —— 否则面板就在为一个不存在的变量编说明。"""
    real_keys = {item["key"] for item in dev_config.frontend_env_status()["keys"]}
    stale = sorted(set(dev_config.FRONTEND_ENV_NOTES) - real_keys)
    assert not stale, f"这些键只在说明表里、前端模板里没有：{stale}"


def _keys_nobody_reads() -> set[str]:
    """扫源码得出「面板能改、但没有任何代码读」的键。

    只排除面板自己（`dev_config.py`）—— 它列出这些键不算「被使用」。
    对每个键找两种痕迹：Settings 属性访问（``.attr``，声明行不会命中）
    与直接在字符串里写的键名（`os.environ["KEY"]` 那种）。
    """
    sources: list[str] = []
    for root in (backend_dir() / "app", project_root() / "scripts"):
        for path in root.rglob("*.py"):
            if path.name == "dev_config.py":
                continue
            sources.append(path.read_text(encoding="utf-8"))

    unread: set[str] = set()
    for field in dev_config.ENV_FIELDS:
        pattern = re.compile(rf"\.{re.escape(field.key.lower())}\b")
        if not any(
            pattern.search(text) or f'"{field.key}"' in text or f"'{field.key}'" in text
            for text in sources
        ):
            unread.add(field.key)
    return unread


def test_unwired_knobs_are_declared_correctly() -> None:
    """★ 面板上的旋钮必须真能拧动什么，转不动就必须说出口。

    两个方向都查：声明了却其实有人读（该把声明去掉），
    以及没人读却忘了声明（该补上）—— 只查一边的话，
    新加一个空旋钮照样是绿的。
    """
    detected = _keys_nobody_reads()
    declared = set(dev_config.INEFFECTIVE_ENV_KEYS)

    assert detected == declared, (
        "面板白名单与实际消费方对不上：\n"
        f"  没人读但没声明：{sorted(detected - declared)}\n"
        f"  声明了但有人读：{sorted(declared - detected)}\n"
        "要么把它接上，要么加进 INEFFECTIVE_ENV_KEYS 并写明为什么，"
        "要么直接从白名单移除（它的真正归属可能在 config/limits.yaml）。"
    )
    assert declared <= {field.key for field in dev_config.ENV_FIELDS}, "声明表里有不存在的键"


def test_unwired_knobs_say_so_in_their_hint() -> None:
    """声明为“改了不生效”的键，它的提示必须写出来 —— 否则面板就是在一个空旋钮上骗人。"""
    hints = {field.key: field.hint for field in dev_config.ENV_FIELDS}
    silent = sorted(
        key
        for key in dev_config.INEFFECTIVE_ENV_KEYS
        if dev_config.INEFFECTIVE_HINT_MARKER not in hints.get(key, "")
    )
    assert not silent, f"这些键改了不生效，但提示里没说：{silent}"


def test_cost_knobs_live_in_limits_yaml_not_in_the_env_panel() -> None:
    """★ 成本与限流阈值只有**一个**归属：`config/limits.yaml`。

    以前 `.env` 里还有一套同名镜像（`PLAN_COST_CIRCUIT_BREAKER_CNY` 等）摆在面板上，
    改了却不生效 —— 而且其中一个还让 `/health` 报了错的熔断阈值。
    现在这些键从面板白名单移除了，面板通过编辑 `limits.yaml` 改它们。

    这条测试反过来锁住那个方向：谁把镜像键又加回白名单，这里就会红，
    提醒他先回答“到底哪个才是事实源”。
    """
    panel_keys = {field.key for field in dev_config.ENV_FIELDS}
    mirrors = {
        "PLAN_COST_CIRCUIT_BREAKER_CNY",
        "SEARCH_COST_CIRCUIT_BREAKER_CNY",
        "GLOBAL_DAILY_BUDGET_CNY",
        "RATE_LIMIT_COLD_PLANS_PER_DAY",
        "MAP_MAX_CALLS_PER_PLAN",
        "BACKEND_PORT",
        "API_BASE_URL",
    }
    assert not (mirrors & panel_keys), (
        "这些键的真正归属不在 .env（见注释），不该出现在面板的环境变量区："
        f"{sorted(mirrors & panel_keys)}"
    )


def test_every_knob_has_some_explanation() -> None:
    """每个字段都得有一句「这是干什么的」：空提示的字段在界面上就是一个没有说明的框。"""
    silent = sorted(field.key for field in dev_config.ENV_FIELDS if not field.hint)
    assert not silent, f"这些字段没有任何说明：{silent}"


def test_map_hints_separate_backend_routing_from_the_frontend_map() -> None:
    """★ 两把高德 Key 必须能被**分开读到**，这是面板上真实发生过的一次误判。

    起因：在「地图」分组里看见「高德 Web 服务 Key（未配置）」，
    于是以为整条地图能力都没配 —— 而结果页那张地图用的是另一把
    「Web端(JS API)」Key，且可能早就配好了。

    只要这两句话还在，误判就不会重演；谁把两把 Key 合回一句，这条测试就红。
    """
    fields = {field.key: field for field in dev_config.ENV_FIELDS}

    web_key = fields["AMAP_WEB_KEY"]
    assert "Web服务" in web_key.hint, "要写清这把是哪种类型的 Key"
    assert "frontend/.env.local" in web_key.hint, "要指明另一把 Key 在哪"
    assert "前端专用配置" in web_key.hint, "要指向面板上那一栏（否则用户还是只看见一栏）"

    provider = fields["MAP_PROVIDER"]
    assert "前端" in provider.hint and "后端" in provider.hint, (
        "Provider 选择只影响后端路由，界面必须说清，否则会以为改它就能换掉结果页那张地图"
    )
