"""Process-local signed continuation for request-bound relationship pages."""
import base64
import hashlib
import hmac
import json
import secrets
from discovery import DiscoveryError
from validation import canonical_json

_KEY=secrets.token_bytes(32)

def encode(binding, state):
    body=canonical_json({"version":1,"binding":binding,"state":state})
    return base64.urlsafe_b64encode(body).decode()+"."+hmac.new(_KEY,body,hashlib.sha256).hexdigest()

def decode(token, binding):
    if token is None: return {"offset":0}
    try:
        if not isinstance(token,str) or len(token)>8192: raise ValueError()
        body_text,signature=token.split(".")
        body=base64.b64decode(body_text,altchars=b"-_",validate=True)
        if not hmac.compare_digest(signature,hmac.new(_KEY,body,hashlib.sha256).hexdigest()): raise ValueError()
        value=json.loads(body)
        if value.get("version")!=1 or value.get("binding")!=binding: raise ValueError()
        state=value["state"]
        if type(state.get("offset")) is not int or not 0<=state["offset"]<=1_000_000: raise ValueError()
        return state
    except (ValueError,TypeError,KeyError,AttributeError):
        raise DiscoveryError("invalid_handle","Continuation is invalid, expired after restart, or belongs to another request",stage="validation") from None
