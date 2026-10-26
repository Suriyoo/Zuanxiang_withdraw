import os
import json
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError, HTTPError

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from database import get_db, init_db


app = FastAPI(title="钻享中心")


# =========================================================
# 基础配置
# =========================================================

AOBI_EXTRACT_URL = os.getenv("AOBI_EXTRACT_URL", "").strip()

# 固定卡密：
# 推荐通过：
# export AOBI_KM="你的卡密"
AOBI_KM = os.getenv("AOBI_KM", "").strip()

# 备用提取接口
AOBI_BACKUP_EXTRACT_URL = os.getenv("AOBI_BACKUP_EXTRACT_URL", "").strip()

AOBI_BACKUP_CARD_CODE = os.getenv(
    "AOBI_BACKUP_CARD_CODE",
    ""
).strip()

# 登录 session 有效时间：30 天
SESSION_TTL = 30 * 24 * 60 * 60


# =========================================================
# Session
# =========================================================

user_sessions = {}
admin_sessions = {}

# 防止同一个用户同时点击多次导致重复提取
user_claim_locks = {}
global_lock = threading.Lock()


def create_user_session(user_id: int):
    token = secrets.token_urlsafe(32)

    user_sessions[token] = {
        "user_id": user_id,
        "created_at": time.time(),
    }

    return token


def get_user_session(request: Request):
    token = request.cookies.get("user_session")

    if not token:
        return None

    session = user_sessions.get(token)

    if not session:
        return None

    if time.time() - session["created_at"] > SESSION_TTL:
        user_sessions.pop(token, None)
        return None

    return session


def require_user(request: Request):
    session = get_user_session(request)

    if not session:
        return None

    return session


def get_user_claim_lock(user_id: int):
    with global_lock:
        if user_id not in user_claim_locks:
            user_claim_locks[user_id] = threading.Lock()

        return user_claim_locks[user_id]


# =========================================================
# 数据库
# =========================================================

init_db()


def init_claim_records_db():
    """创建提取记录表（仅首次启动时真正创建）。"""
    db = get_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                reward_code TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reward_claims_created_at
            ON reward_claims(created_at DESC)
            """
        )

        db.commit()
    finally:
        db.close()


init_claim_records_db()


# =========================================================
# 普通用户登录
# =========================================================

@app.post("/api/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"success": False, "message": "请求格式错误"},
            status_code=400
        )

    username = str(data.get("username", "")).strip()

    if not username:
        return JSONResponse(
            {"success": False, "message": "请输入用户名"},
            status_code=400
        )

    db = get_db()

    try:
        cursor = db.execute(
            """
            SELECT id, username, diamonds
            FROM users
            WHERE username = ?
            """,
            (username,)
        )

        user = cursor.fetchone()

        if not user:
            return JSONResponse(
                {"success": False, "message": "用户不存在"},
                status_code=404
            )

        user_id = user["id"]

    finally:
        db.close()

    token = create_user_session(user_id)

    response = JSONResponse({
        "success": True,
        "username": user["username"],
        "diamonds": user["diamonds"]
    })

    response.set_cookie(
        key="user_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=SESSION_TTL
    )

    return response


# =========================================================
# 当前登录用户
# =========================================================

@app.get("/api/me")
async def current_user(request: Request):
    session = require_user(request)

    if not session:
        return JSONResponse(
            {"success": False, "message": "未登录"},
            status_code=401
        )

    db = get_db()

    try:
        cursor = db.execute(
            """
            SELECT id, username, diamonds
            FROM users
            WHERE id = ?
            """,
            (session["user_id"],)
        )

        user = cursor.fetchone()

    finally:
        db.close()

    if not user:
        return JSONResponse(
            {"success": False, "message": "用户不存在"},
            status_code=401
        )

    return {
        "success": True,
        "username": user["username"],
        "diamonds": user["diamonds"]
    }


# =========================================================
# 用户退出
# =========================================================

@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get("user_session")

    if token:
        user_sessions.pop(token, None)

    response = JSONResponse({
        "success": True
    })

    response.delete_cookie("user_session")

    return response


# =========================================================
# 调用外部提取 ID
# =========================================================

def extract_external_id():
    """
    使用服务器端固定卡密调用：
    http://8.130.43.77/admin/api/extract_id.php?km=xxxx

    返回：
    {
        "code": 200,
        "msg": "提取成功",
        "data": "312766755"
    }
    """

    if not AOBI_KM:
        raise RuntimeError("服务器没有配置 AOBI_KM")

    url = f"{AOBI_EXTRACT_URL}?km={quote(AOBI_KM)}"

    request = UrlRequest(url)
    request.add_header(
        "User-Agent",
        "Zuanxiang/1.0"
    )

    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")

    except HTTPError as e:
        raise RuntimeError(f"外部接口 HTTP 错误：{e.code}")

    except URLError as e:
        raise RuntimeError(f"外部接口连接失败：{e.reason}")

    except Exception as e:
        raise RuntimeError(f"外部接口请求失败：{str(e)}")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("外部接口返回的数据不是 JSON")

    # 注意：
    # 外部接口即使 HTTP 200，
    # code 也可能是 400。
    if result.get("code") != 200:
        message = result.get("msg", "提取失败")
        raise RuntimeError(message)

    external_id = str(result.get("data", "")).strip()

    if not external_id:
        raise RuntimeError("外部接口没有返回 ID")

    return external_id


def extract_backup_id():
    """
    使用备用接口提取 ID。

    备用服务的前端最终会 POST 到：
    /extract_auth_code

    请求参数：
    card_code=AOBI_BACKUP_CARD_CODE

    成功返回：
    {
        "success": true,
        "auth_code": "...",
        "message": "..."
    }
    """

    if not AOBI_BACKUP_CARD_CODE:
        raise RuntimeError("服务器没有配置备用接口卡密")

    form_data = urlencode({
        "card_code": AOBI_BACKUP_CARD_CODE
    }).encode("utf-8")

    request = UrlRequest(
        AOBI_BACKUP_EXTRACT_URL,
        data=form_data,
        method="POST"
    )
    request.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded"
    )
    request.add_header(
        "User-Agent",
        "Zuanxiang/1.0"
    )

    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8")

    except HTTPError as e:
        raise RuntimeError(f"备用接口 HTTP 错误：{e.code}")

    except URLError as e:
        raise RuntimeError(f"备用接口连接失败：{e.reason}")

    except Exception as e:
        raise RuntimeError(f"备用接口请求失败：{str(e)}")

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("备用接口返回的数据不是 JSON")

    if result.get("success") is not True:
        message = result.get(
            "message",
            "备用接口提取失败"
        )
        raise RuntimeError(str(message))

    external_id = str(
        result.get("auth_code", "")
    ).strip().lstrip("#").strip()

    if not external_id:
        raise RuntimeError(
            "备用接口没有返回 auth_code"
        )

    return external_id

# =========================================================
# 提取钻享
# =========================================================

@app.post("/api/reward/claim")
async def claim_reward(request: Request):
    session = require_user(request)

    if not session:
        return JSONResponse(
            {
                "success": False,
                "message": "请先登录"
            },
            status_code=401
        )

    user_id = session["user_id"]

    # 防止同一个用户连续快速点击
    lock = get_user_claim_lock(user_id)

    if not lock.acquire(blocking=False):
        return JSONResponse(
            {
                "success": False,
                "message": "正在提取，请稍候"
            },
            status_code=429
        )

    try:
        # -------------------------------------------------
        # 1. 检查用户钻享数量
        # -------------------------------------------------

        db = get_db()

        try:
            cursor = db.execute(
                """
                SELECT id, username, diamonds
                FROM users
                WHERE id = ?
                """,
                (user_id,)
            )

            user = cursor.fetchone()

        finally:
            db.close()

        if not user:
            return JSONResponse(
                {
                    "success": False,
                    "message": "用户不存在"
                },
                status_code=401
            )

        if user["diamonds"] <= 0:
            return JSONResponse(
                {
                    "success": False,
                    "message": "钻享不足"
                },
                status_code=400
            )

        # -------------------------------------------------
        # 2. 调用外部固定卡密
        # -------------------------------------------------

        try:
            # 先调用主接口
            external_id = extract_external_id()

        except Exception as primary_error:
            import traceback
            print("========== 主接口提取异常 ==========")
            traceback.print_exc()
            print("====================================")

            # 主接口失败，自动切换备用接口
            try:
                external_id = extract_backup_id()

            except Exception as backup_error:
                return JSONResponse(
                    {
                        "success": False,
                        "message": (
                            f"主接口提取失败：{primary_error}；"
                            f"备用接口提取失败：{backup_error}"
                        )
                    },
                    status_code=502
                )

        # -------------------------------------------------
        # 3. 外部提取成功之后，才扣除 1 个钻享
        # -------------------------------------------------

        db = get_db()

        try:
            cursor = db.execute(
                """
                UPDATE users
                SET diamonds = diamonds - 1
                WHERE id = ?
                  AND diamonds > 0
                """,
                (user_id,)
            )

            if cursor.rowcount != 1:
                db.rollback()
                return JSONResponse(
                    {
                        "success": False,
                        "message": "钻享不足，请重新提取"
                    },
                    status_code=400
                )

            claim_time = int(time.time())

            # 与扣除钻享放在同一个事务中：
            # 只有数据库同时完成扣钻和写入记录，才提交。
            db.execute(
                """
                INSERT INTO reward_claims
                    (username, reward_code, created_at)
                VALUES (?, ?, ?)
                """,
                (user["username"], external_id, claim_time)
            )

            # 普通用户自己的提取记录
            db.execute(
                """
                INSERT INTO claim_records
                    (user_id, diamonds, claimed_at, external_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    1,
                    datetime.fromtimestamp(claim_time).strftime("%Y-%m-%d %H:%M:%S"),
                    external_id
                )
            )

            cursor = db.execute(
                """
                SELECT diamonds
                FROM users
                WHERE id = ?
                """,
                (user_id,)
            )

            updated_user = cursor.fetchone()

            db.commit()

        finally:
            db.close()

        # -------------------------------------------------
        # 4. 保存本次提取结果到 Session
        # -------------------------------------------------

        session["last_reward"] = {
            "id": external_id,
            "created_at": time.time()
        }

        return {
            "success": True,
            "id": external_id,
            "diamonds": updated_user["diamonds"],
            "reward_url": "/reward.html"
        }

    finally:
        lock.release()


# =========================================================
# 获取最近一次提取结果
# =========================================================

@app.get("/api/reward/result")
async def reward_result(request: Request):
    session = require_user(request)

    if not session:
        return JSONResponse(
            {
                "success": False,
                "message": "请先登录"
            },
            status_code=401
        )

    reward = session.get("last_reward")

    if not reward:
        return JSONResponse(
            {
                "success": False,
                "message": "没有找到提取结果"
            },
            status_code=404
        )

    return {
        "success": True,
        "id": reward["id"]
    }


# =========================================================
# 普通用户提取记录
# =========================================================

@app.get("/api/reward/records")
async def reward_records(request: Request):
    session = require_user(request)

    if not session:
        return JSONResponse(
            {
                "success": False,
                "message": "请先登录"
            },
            status_code=401
        )

    user_id = session["user_id"]

    db = get_db()

    try:
        rows = db.execute(
            """
            SELECT
                id,
                diamonds,
                claimed_at,
                external_id
            FROM claim_records
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 3
            """,
            (user_id,)
        ).fetchall()

        records = [
            {
                "id": row["id"],
                "diamonds": row["diamonds"],
                "claimed_at": row["claimed_at"],
                "external_id": row["external_id"]
            }
            for row in rows
        ]

        return {
            "success": True,
            "records": records
        }

    finally:
        db.close()


# =========================================================
# 管理员登录
# =========================================================

ADMIN_USERNAME = os.getenv(
    "ADMIN_USERNAME",
    "admin"
)

ADMIN_PASSWORD = os.getenv(
    "ADMIN_PASSWORD",
    ""
).strip()

# 管理员登录失败限制
ADMIN_LOGIN_MAX_FAILURES = 5
ADMIN_LOGIN_LOCK_SECONDS = 600
admin_login_failures = {}


@app.post("/api/admin/login")
async def admin_login(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {
                "success": False,
                "message": "请求格式错误"
            },
            status_code=400
        )

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not ADMIN_PASSWORD:
        return JSONResponse(
            {
                "success": False,
                "message": "管理员密码尚未配置，请联系管理员"
            },
            status_code=503
        )

    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    failure_info = admin_login_failures.get(client_ip)

    if failure_info:
        failed_count = failure_info.get("count", 0)
        locked_until = failure_info.get("locked_until", 0)

        if locked_until > now:
            remaining = int(locked_until - now) + 1

            return JSONResponse(
                {
                    "success": False,
                    "message": f"登录失败次数过多，请 {remaining} 秒后再试"
                },
                status_code=429
            )

    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        failure_info = admin_login_failures.get(
            client_ip,
            {
                "count": 0,
                "locked_until": 0
            }
        )

        failure_info["count"] += 1

        if failure_info["count"] >= ADMIN_LOGIN_MAX_FAILURES:
            failure_info["locked_until"] = (
                now + ADMIN_LOGIN_LOCK_SECONDS
            )

        admin_login_failures[client_ip] = failure_info

        return JSONResponse(
            {
                "success": False,
                "message": "用户名或密码错误"
            },
            status_code=401
        )

    # 登录成功后清除该 IP 的失败记录
    admin_login_failures.pop(client_ip, None)

    token = secrets.token_urlsafe(32)

    admin_sessions[token] = {
        "created_at": time.time(),
        "username": username
    }

    response = JSONResponse({
        "success": True
    })

    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=SESSION_TTL
    )

    return response


def require_admin(request: Request):
    token = request.cookies.get("admin_session")

    if not token:
        return False

    session = admin_sessions.get(token)

    if not session:
        return False

    if time.time() - session["created_at"] > SESSION_TTL:
        admin_sessions.pop(token, None)
        return False

    return True


@app.get("/api/admin/me")
async def admin_me(request: Request):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "message": "未登录"
            },
            status_code=401
        )

    token = request.cookies.get("admin_session")
    session = admin_sessions.get(token)

    return {
        "success": True,
        "username": session["username"]
    }


@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get("admin_session")

    if token:
        admin_sessions.pop(token, None)

    response = JSONResponse({
        "success": True
    })

    response.delete_cookie("admin_session")

    return response


# =========================================================
# 管理员：提取钻享记录
# =========================================================

@app.get("/api/admin/reward-claims")
async def admin_reward_claims(
    request: Request,
    page: int = 1
):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "message": "未登录"
            },
            status_code=401
        )

    PAGE_SIZE = 8
    RETENTION_SECONDS = 7 * 24 * 60 * 60
    cutoff = int(time.time()) - RETENTION_SECONDS

    if page < 1:
        page = 1

    db = get_db()

    try:
        # 每次管理员读取记录时清理超过 7 天的数据。
        db.execute(
            "DELETE FROM reward_claims WHERE created_at < ?",
            (cutoff,)
        )
        db.commit()

        cursor = db.execute(
            "SELECT COUNT(*) AS total FROM reward_claims"
        )
        total = cursor.fetchone()["total"]

        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

        if page > total_pages:
            page = total_pages

        offset = (page - 1) * PAGE_SIZE

        cursor = db.execute(
            """
            SELECT username, reward_code, created_at
            FROM reward_claims
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (PAGE_SIZE, offset)
        )

        records = []
        for row in cursor.fetchall():
            records.append({
                "username": row["username"],
                "reward_code": row["reward_code"],
                "created_at": datetime.fromtimestamp(
                    row["created_at"]
                ).strftime("%Y-%m-%d %H:%M:%S")
            })

    finally:
        db.close()

    return {
        "success": True,
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "total_pages": total_pages,
        "records": records
    }


# =========================================================
# 管理员：用户列表
# =========================================================

@app.get("/api/admin/users")
async def admin_users(request: Request):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "message": "未登录"
            },
            status_code=401
        )

    db = get_db()

    try:
        cursor = db.execute(
            """
            SELECT id, username, diamonds, is_admin
            FROM users
            ORDER BY id
            """
        )

        users = [dict(row) for row in cursor.fetchall()]

    finally:
        db.close()

    return {
        "success": True,
        "users": users
    }


# =========================================================
# 管理员：创建用户
# =========================================================

@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "message": "未登录"
            },
            status_code=401
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {
                "success": False,
                "message": "请求格式错误"
            },
            status_code=400
        )

    username = str(data.get("username", "")).strip()

    try:
        diamonds = int(data.get("diamonds", 0))
    except Exception:
        diamonds = 0

    if not username:
        return JSONResponse(
            {
                "success": False,
                "message": "用户名不能为空"
            },
            status_code=400
        )

    if diamonds < 0:
        return JSONResponse(
            {
                "success": False,
                "message": "钻享不能小于 0"
            },
            status_code=400
        )

    db = get_db()

    try:
        cursor = db.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,)
        )

        if cursor.fetchone():
            return JSONResponse(
                {
                    "success": False,
                    "message": "用户名已存在"
                },
                status_code=400
            )

        db.execute(
            """
            INSERT INTO users (username, diamonds, is_admin)
            VALUES (?, ?, 0)
            """,
            (username, diamonds)
        )

        db.commit()

    finally:
        db.close()

    return {
        "success": True
    }


# =========================================================
# 管理员：修改用户
# =========================================================

@app.put("/api/admin/users/{user_id}")
async def admin_update_user(
    user_id: int,
    request: Request
):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "message": "未登录"
            },
            status_code=401
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {
                "success": False,
                "message": "请求格式错误"
            },
            status_code=400
        )

    username = str(data.get("username", "")).strip()

    try:
        diamonds = int(data.get("diamonds", 0))
    except Exception:
        diamonds = 0

    if not username:
        return JSONResponse(
            {
                "success": False,
                "message": "用户名不能为空"
            },
            status_code=400
        )

    if diamonds < 0:
        return JSONResponse(
            {
                "success": False,
                "message": "钻享不能小于 0"
            },
            status_code=400
        )

    db = get_db()

    try:
        cursor = db.execute(
            """
            SELECT id
            FROM users
            WHERE username = ?
              AND id != ?
            """,
            (username, user_id)
        )

        if cursor.fetchone():
            return JSONResponse(
                {
                    "success": False,
                    "message": "用户名已存在"
                },
                status_code=400
            )

        cursor = db.execute(
            """
            UPDATE users
            SET username = ?, diamonds = ?
            WHERE id = ?
            """,
            (username, diamonds, user_id)
        )

        db.commit()

        if cursor.rowcount != 1:
            return JSONResponse(
                {
                    "success": False,
                    "message": "用户不存在"
                },
                status_code=404
            )

    finally:
        db.close()

    return {
        "success": True
    }


# =========================================================
# 管理员：删除用户
# =========================================================

@app.patch("/api/admin/users/{username}/diamonds")
async def admin_update_user_diamonds(
    username: str,
    request: Request
):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "detail": "未登录或登录已过期"
            },
            status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {
                "success": False,
                "detail": "请求数据无效"
            },
            status_code=400
        )

    diamonds = body.get("diamonds")

    if (
        isinstance(diamonds, bool)
        or not isinstance(diamonds, int)
        or diamonds < 0
    ):
        return JSONResponse(
            {
                "success": False,
                "detail": "钻享数量必须是大于等于 0 的整数"
            },
            status_code=400
        )

    db = get_db()

    try:
        user = db.execute(
            """
            SELECT id, username
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        if not user:
            return JSONResponse(
                {
                    "success": False,
                    "detail": "用户不存在"
                },
                status_code=404
            )

        db.execute(
            """
            UPDATE users
            SET diamonds = ?
            WHERE id = ?
            """,
            (diamonds, user["id"])
        )

        db.commit()

        return {
            "success": True,
            "username": user["username"],
            "diamonds": diamonds
        }

    finally:
        db.close()


@app.delete("/api/admin/users/{username}")
async def admin_delete_user(
    username: str,
    request: Request
):
    if not require_admin(request):
        return JSONResponse(
            {
                "success": False,
                "message": "未登录"
            },
            status_code=401
        )

    db = get_db()

    try:
        cursor = db.execute(
            "SELECT id, username, is_admin FROM users WHERE username = ?",
            (username,)
        )

        user = cursor.fetchone()

        if not user:
            return JSONResponse(
                {
                    "success": False,
                    "message": "用户不存在"
                },
                status_code=404
            )

        if user["is_admin"]:
            return JSONResponse(
                {
                    "success": False,
                    "message": "不能删除管理员"
                },
                status_code=400
            )

        db.execute(
            "DELETE FROM users WHERE id = ?",
            (user["id"],)
        )

        db.commit()

    finally:
        db.close()

    return {
        "success": True
    }


# =========================================================
# 页面
# =========================================================

@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/admin")
def admin_page():
    return FileResponse("static/admin.html")


@app.get("/reward.html")
def reward_page(request: Request):
    """
    重点：
    reward.html 不允许未登录用户直接访问。
    """

    if not require_user(request):
        return JSONResponse(
            {
                "success": False,
                "message": "请先登录"
            },
            status_code=401
        )

    return FileResponse("static/reward.html")


app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)