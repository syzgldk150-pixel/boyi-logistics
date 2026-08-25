"""飞书 WebSocket 长连接客户端"""

import os
import asyncio
import logging
import threading
import socket

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows dev fallback
    fcntl = None

logger = logging.getLogger("feishu")

_ws_client = None
_agent_core = None
_agent_loop = None
_ws_thread = None
_running = False
_lease_conn = None
_lease_lock_fd = None
_lease_owner = ""
_LEASE_NAME = "logistics_agent_feishu_ws_consumer"


def bind_agent_runtime(agent_core, agent_loop=None):
    """绑定飞书事件处理所需的 Agent 运行时上下文。"""
    global _agent_core, _agent_loop
    _agent_core = agent_core
    if agent_loop is not None:
        _agent_loop = agent_loop


def feishu_event_mode() -> str:
    mode = str(os.getenv("FEISHU_EVENT_MODE", "websocket") or "websocket").strip().lower()
    return mode or "websocket"


def websocket_enabled() -> bool:
    return feishu_event_mode() in {"websocket", "ws"}


def websocket_lease_active() -> bool:
    if str(os.getenv("FEISHU_WS_LEASE_DISABLED") or "").strip().lower() in {"1", "true", "yes"}:
        return bool(_running)
    return _lease_conn is not None or _lease_lock_fd is not None


def _db_connect_for_lease():
    import pymysql

    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=True,
    )


def _local_lock_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_dir = os.path.join(root, "agent", "tms_runtime", "state")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "feishu_ws.lock")


def _acquire_local_lease(owner: str) -> bool:
    global _lease_lock_fd
    if fcntl is None:
        logger.warning("fcntl 不可用，跳过本机 Feishu WebSocket 文件锁")
        return True
    lock_fd = os.open(_local_lock_path(), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        logger.warning("Feishu WebSocket 本机租约已被其他实例持有，跳过启动")
        return False
    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, owner.encode("utf-8", errors="ignore"))
    _lease_lock_fd = lock_fd
    logger.info("Feishu WebSocket 本机租约已获取 owner=%s", owner)
    return True


def _acquire_ws_lease() -> bool:
    global _lease_conn, _lease_owner
    if str(os.getenv("FEISHU_WS_LEASE_DISABLED") or "").strip().lower() in {"1", "true", "yes"}:
        logger.warning("Feishu WebSocket 租约已禁用")
        return True
    owner = f"{socket.gethostname()}:{os.getpid()}"
    try:
        conn = _db_connect_for_lease()
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0)", (_LEASE_NAME,))
            row = cur.fetchone()
        acquired = bool(row and int(row[0]) == 1)
        if acquired:
            _lease_conn = conn
            _lease_owner = owner
            logger.info("Feishu WebSocket MySQL 租约已获取 owner=%s", owner)
            return True
        conn.close()
        logger.warning("Feishu WebSocket MySQL 租约已被其他实例持有，跳过启动 owner=%s", owner)
        return False
    except Exception as exc:
        logger.warning("Feishu WebSocket MySQL 租约获取失败，降级本机锁: %s", str(exc)[:160])
        _lease_owner = owner
        return _acquire_local_lease(owner)


def _release_ws_lease() -> None:
    global _lease_conn, _lease_lock_fd, _lease_owner
    if _lease_conn is not None:
        try:
            with _lease_conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (_LEASE_NAME,))
        except Exception as exc:
            logger.warning("Feishu WebSocket MySQL 租约释放失败: %s", str(exc)[:160])
        try:
            _lease_conn.close()
        except Exception:
            pass
        _lease_conn = None
        logger.info("Feishu WebSocket MySQL 租约已释放 owner=%s", _lease_owner)
    if _lease_lock_fd is not None:
        try:
            if fcntl is not None:
                fcntl.flock(_lease_lock_fd, fcntl.LOCK_UN)
            os.close(_lease_lock_fd)
        except Exception:
            pass
        _lease_lock_fd = None
        logger.info("Feishu WebSocket 本机租约已释放 owner=%s", _lease_owner)
    _lease_owner = ""


async def start_feishu_ws(agent_core):
    """启动飞书 WebSocket 长连接"""
    global _ws_thread, _running

    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    current_loop = asyncio.get_running_loop()

    bind_agent_runtime(agent_core, current_loop)

    if not websocket_enabled():
        logger.info("飞书事件模式=%s，跳过 WebSocket 长连接", feishu_event_mode())
        return

    if not app_id or not app_secret:
        logger.warning("飞书 App ID/Secret 未配置，跳过飞书连接")
        return

    if _running and _ws_thread and _ws_thread.is_alive():
        logger.info("飞书 WebSocket 已在运行，跳过重复启动")
        return

    if not _acquire_ws_lease():
        agent_core.set_feishu_connected(False)
        return

    # Importing lark-oapi cold loads a large generated model tree while holding
    # the interpreter lock.  Complete that work before FastAPI reports startup
    # readiness so the release identity probe cannot be starved by the
    # background WebSocket thread.
    try:
        await asyncio.to_thread(_load_ws_dependencies)
    except ImportError:
        logger.error("lark-oapi 未安装，无法启动飞书连接")
        agent_core.set_feishu_connected(False)
        _release_ws_lease()
        return
    except Exception as exc:
        logger.error("飞书 WebSocket 依赖加载失败: %s", type(exc).__name__)
        agent_core.set_feishu_connected(False)
        _release_ws_lease()
        return

    _running = True
    _ws_thread = threading.Thread(
        target=_run_ws_client,
        args=(app_id, app_secret),
        name="feishu-ws",
        daemon=True,
    )
    _ws_thread.start()
    logger.info("飞书 WebSocket 后台线程已启动")


async def stop_feishu_ws():
    """停止飞书连接"""
    global _running
    _running = False
    if _agent_core:
        _agent_core.set_feishu_connected(False)
    _release_ws_lease()
    logger.info("飞书 WebSocket 已断开")


def get_agent_core():
    return _agent_core


def get_agent_loop():
    return _agent_loop


def _set_feishu_connected(connected: bool):
    if _agent_core:
        _agent_core.set_feishu_connected(connected)


def _set_feishu_connected_threadsafe(connected: bool):
    if _agent_loop and _agent_loop.is_running():
        _agent_loop.call_soon_threadsafe(_set_feishu_connected, connected)
    else:
        _set_feishu_connected(connected)


def _load_ws_dependencies():
    """Load the SDK and handlers before service startup is declared ready."""

    import lark_oapi as lark
    import lark_oapi.ws.client as ws_client_module

    from feishu.message_handler import handle_bot_menu, handle_im_message

    return lark, ws_client_module, handle_bot_menu, handle_im_message


def _run_ws_client(app_id: str, app_secret: str):
    """在独立线程里启动 lark-oapi，避免复用 uvicorn 主事件循环。"""
    global _ws_client, _running

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        lark, ws_client_module, handle_bot_menu, handle_im_message = (
            _load_ws_dependencies()
        )

        # lark_oapi.ws.client 在 import 时会缓存模块级事件循环；
        # 这里强制切到当前线程的新 loop，避免命中 uvicorn 主循环。
        ws_client_module.loop = loop

        _ws_client = lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(handle_im_message)
            .register_p2_application_bot_menu_v6(handle_bot_menu)
            .build(),
            # SDK connection logs can include an access_key query value before
            # our logging filters can redact it. Only allow CRITICAL SDK output;
            # application lifecycle logs remain available through our logger.
            log_level=lark.LogLevel.CRITICAL,
        )

        logger.info("飞书 WebSocket 连接已建立")
        _set_feishu_connected_threadsafe(True)
        _ws_client.start()

    except ImportError:
        logger.error("lark-oapi 未安装，无法启动飞书连接")
        _set_feishu_connected_threadsafe(False)
    except Exception as e:
        logger.error("飞书连接失败: %s", str(e)[:200])
        _set_feishu_connected_threadsafe(False)
    finally:
        _running = False
        _release_ws_lease()
