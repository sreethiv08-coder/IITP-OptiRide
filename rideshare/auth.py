
import hashlib
import hmac
import json
import secrets
import threading
import re
from datetime import datetime, timedelta, timezone

from .config import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "outputs"
USERS_FILE = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"

LOCK = threading.RLock()
SESSION_DAYS = 7
ITERATIONS = 600_000

VALID_GENDERS = {"FEMALE", "MALE", "OTHER"}


class AuthError(Exception):
    pass


def _read(path):
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        raise AuthError("Could not read account data.")


def _write(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        ITERATIONS
    ).hex()


def _public_user(user):
    return {
        "name": user["name"],
        "roll_number": user["roll_number"],
        "contact_number": user.get("contact_number", ""),
        "gender": user.get("gender", "")
    }


def register(name, roll_number, password, contact_number, gender):
    name = str(name or "").strip()
    roll_number = str(roll_number or "").strip().upper()
    password = str(password or "")
    contact_number = str(contact_number or "").strip()
    gender = str(gender or "").strip().upper()

    if not all((name, roll_number, password, contact_number, gender)):
        raise AuthError("Please fill in all registration fields.")

    if len(password) < 8:
        raise AuthError("Password must contain at least 8 characters.")

    if len(name) > 100 or len(roll_number) > 30:
        raise AuthError("Name or roll number is too long.")

    if not re.fullmatch(r"[0-9+\-\s()]{10,20}", contact_number):
        raise AuthError("Please enter a valid contact number.")

    digits = re.sub(r"\D", "", contact_number)
    if not 10 <= len(digits) <= 15:
        raise AuthError("Contact number must contain 10 to 15 digits.")

    if gender not in VALID_GENDERS:
        raise AuthError("Please select a valid gender.")

    with LOCK:
        users = _read(USERS_FILE)

        if roll_number in users:
            raise AuthError(
                "An account with this roll number already exists."
            )

        salt = secrets.token_bytes(16).hex()

        users[roll_number] = {
            "name": name,
            "roll_number": roll_number,
            "contact_number": contact_number,
            "gender": gender,
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        _write(USERS_FILE, users)

        return _public_user(users[roll_number])


def authenticate(roll_number, password):
    roll_number = str(roll_number or "").strip().upper()
    password = str(password or "")

    with LOCK:
        users = _read(USERS_FILE)
        user = users.get(roll_number)

        if not user:
            raise AuthError("Incorrect roll number or password.")

        attempted = _hash_password(password, user["salt"])

        if not hmac.compare_digest(
            attempted,
            user["password_hash"]
        ):
            raise AuthError("Incorrect roll number or password.")

        return _public_user(user)


def create_session(roll_number):
    roll_number = str(roll_number or "").strip().upper()

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    with LOCK:
        users = _read(USERS_FILE)

        if roll_number not in users:
            raise AuthError("Account not found.")

        sessions = _read(SESSIONS_FILE)
        now = datetime.now(timezone.utc)

        sessions[token_hash] = {
            "roll_number": roll_number,
            "expires_at": (
                now + timedelta(days=SESSION_DAYS)
            ).isoformat()
        }

        _write(SESSIONS_FILE, sessions)

    return token


def get_session_user(token):
    if not token:
        return None

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    with LOCK:
        sessions = _read(SESSIONS_FILE)
        session = sessions.get(token_hash)

        if not session:
            return None

        expiry = datetime.fromisoformat(session["expires_at"])

        if expiry <= datetime.now(timezone.utc):
            sessions.pop(token_hash, None)
            _write(SESSIONS_FILE, sessions)
            return None

        users = _read(USERS_FILE)
        user = users.get(session["roll_number"])

        return _public_user(user) if user else None


def delete_session(token):
    if not token:
        return

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    with LOCK:
        sessions = _read(SESSIONS_FILE)
        sessions.pop(token_hash, None)
        _write(SESSIONS_FILE, sessions)
