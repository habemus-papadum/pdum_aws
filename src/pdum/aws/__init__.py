"""AWS utils.

Small, reusable helpers over ``boto3``:

* :mod:`pdum.aws.core` — sessions and clients on the ambient credential chain.
* :mod:`pdum.aws.secrets` — a namespaced view of SSM Parameter Store.
* :mod:`pdum.aws.quotas` — inspect Service Quotas and request increases.
* :mod:`pdum.aws.env` — build a process environment from ``.env`` and SSM.

Nothing here hardcodes a profile, region, or account. Credentials come from
``boto3``'s own resolution, so an account is selected by setting
``AWS_PROFILE`` in the environment:

```console
$ AWS_PROFILE=my-account python -m my_script
```

```python
from pdum import aws

print(aws.whoami()["Account"])
```

A script that wants the environment ``pdx`` would have given it — the project's
``.env``, its ``.env.local`` overlay, and the secrets on the SSM search path —
calls :func:`~pdum.aws.env.load_env` in place of ``dotenv.load_dotenv()``:

```python
from pdum.aws import load_env

load_env()
```
"""

from pdum.aws.core import client, resource, session, whoami
from pdum.aws.env import EnvReport, NoSearchPath, load_env

__version__ = "0.6.0"

__all__ = [
    "EnvReport",
    "NoSearchPath",
    "__version__",
    "client",
    "load_env",
    "resource",
    "session",
    "whoami",
]
