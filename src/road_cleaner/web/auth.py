"""Who is asking.

Firebase Authentication, Google sign-in only. The browser does the signing in
and holds an ID token; every request that needs an identity carries it as
``Authorization: Bearer <token>``. This module's whole job is to turn that
string back into a person, or to refuse.

Three things worth knowing before changing anything here:

1. **The token is verified, not trusted.** ``verify_id_token`` checks Google's
   signature, the audience (this project, not somebody else's), and the expiry.
   An email address that arrives any other way -- a request body, a query
   parameter, a header we invented -- is a string a caller typed, and this
   application sends mail to addresses. The distinction is the security model.

2. **There is no session.** No cookie, no server-side store, nothing on
   ``app.state``. That is deliberate: the job registries next door are already
   in-process and already pin the deployment to ``--max-instances=1``, and
   adding a second reason to be un-scalable for something a stateless bearer
   token does perfectly well would be a poor trade.

   It has one honest consequence: a server-side redirect cannot gate an HTML
   page, because the token lives in JavaScript and the browser does not send it
   with a document request. So pages gate themselves client-side for looks, and
   the API routes below gate for real. The page being readable is not the same
   as the actions being available.

3. **Unconfigured is a supported state.** With no FIREBASE_* settings the site
   runs as it did before accounts existed: no sign-in, no saved incidents. So
   nothing here may raise at import time or on a cold path -- ``require_user``
   returns a 501 that says which settings are missing, rather than a 500.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException, Request

from road_cleaner.config import Settings, get_settings

log = logging.getLogger(__name__)

# Initialised once, lazily, on the first request that needs it. Module-level
# rather than on `app.state` because `firebase_admin` keeps its own global app
# registry anyway -- holding a second handle somewhere else would not make it
# less of a singleton, only harder to find.
_app: object | None = None
_init_failed = False


class AuthUnavailable(HTTPException):
    """Sign-in is not configured on this deployment.

    A 501 rather than a 401: the caller did nothing wrong and no credential
    would help. The detail names the missing settings, because the person who
    hits this is almost always the person who can fix it.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=501, detail=detail)


@dataclass(frozen=True)
class AuthUser:
    """A verified identity.

    Frozen because a route handler that could rewrite ``email`` is a route
    handler that could mail somewhere Google never vouched for.
    """

    uid: str
    email: str | None
    email_verified: bool
    name: str | None
    picture: str | None

    @property
    def mailable(self) -> str | None:
        """The address this application is willing to write to, if any.

        Verified only. Google sign-in always satisfies this; the check stays
        because the day somebody enables a second provider it is the difference
        between mailing a person and mailing whatever they typed.
        """
        if self.email and self.email_verified:
            return self.email
        return None


def _firebase(settings: Settings):
    """The initialised firebase_admin app, or None if it cannot be had."""
    global _app, _init_failed

    if _app is not None:
        return _app
    if _init_failed:
        return None
    if not settings.firebase_project_id:
        return None

    try:
        import firebase_admin
    except ImportError:
        _init_failed = True
        log.warning(
            "FIREBASE_PROJECT_ID is set but firebase-admin is not installed. "
            'Install the cloud extra: pip install -e ".[cloud]"'
        )
        return None

    try:
        # `projectId` explicitly rather than letting it be inferred: token
        # verification checks the audience against it, and inferring it from
        # ambient ADC is how you end up verifying tokens against a different
        # project than the one the browser signed in to.
        #
        # get_app first because uvicorn --reload re-imports this module while
        # the previous app object is still in firebase_admin's registry, and
        # initialize_app would raise ValueError on the duplicate name.
        try:
            _app = firebase_admin.get_app()
        except ValueError:
            _app = firebase_admin.initialize_app(
                options={"projectId": settings.firebase_project_id}
            )
    except Exception as exc:  # pragma: no cover - depends on ambient credentials
        _init_failed = True
        log.warning("Could not initialise Firebase: %s", exc)
        return None

    return _app


def verify_id_token(token: str, settings: Settings | None = None) -> AuthUser | None:
    """Turn a bearer token into a person. None if it is not a valid one.

    Every failure mode -- expired, revoked, malformed, signed for a different
    project -- collapses to None on purpose. The caller's response is the same
    in all of them, and an error message that distinguishes "expired" from
    "forged" tells an attacker which half of the problem they solved.
    """
    settings = settings or get_settings()
    app = _firebase(settings)
    if app is None:
        return None

    from firebase_admin import auth as fb_auth

    try:
        claims = fb_auth.verify_id_token(token, app=app)
    except Exception as exc:
        log.debug("Rejected an ID token: %s", exc)
        return None

    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        return None

    return AuthUser(
        uid=str(uid),
        email=claims.get("email"),
        email_verified=bool(claims.get("email_verified")),
        name=claims.get("name"),
        picture=claims.get("picture"),
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def current_user(request: Request) -> AuthUser | None:
    """Whoever is signed in, or None. Never raises.

    For routes that behave differently when signed in but work either way.
    """
    token = _bearer(request)
    if not token:
        return None
    return verify_id_token(token, request.app.state.container.settings)


def require_user(request: Request) -> AuthUser:
    """Whoever is signed in. 401 otherwise.

    Use as a FastAPI dependency: ``user: AuthUser = Depends(require_user)``.
    """
    settings = request.app.state.container.settings

    if not settings.auth_configured:
        raise AuthUnavailable(
            "Sign-in is not configured on this deployment. Set FIREBASE_PROJECT_ID, "
            "FIREBASE_API_KEY, FIREBASE_AUTH_DOMAIN and FIREBASE_APP_ID."
        )

    token = _bearer(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Sign in first. This endpoint needs an Authorization: Bearer <ID token> header.",
        )

    user = verify_id_token(token, settings)
    if user is None:
        raise HTTPException(
            status_code=401, detail="That sign-in is not valid any more. Sign in again."
        )
    return user


def require_mailable_user(request: Request) -> AuthUser:
    """A signed-in user with an address this application may write to.

    Separate from `require_user` so the two refusals stay distinguishable: one
    means "sign in", the other means "your provider never verified that address
    and we will not mail it".
    """
    user = require_user(request)
    if user.mailable is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "Your account has no verified email address, so there is nowhere "
                "to send the report."
            ),
        )
    return user
