"""AWS utils.

Small, reusable helpers over ``boto3``:

* :mod:`pdum.aws.core` — sessions and clients on the ambient credential chain.
* :mod:`pdum.aws.secrets` — a namespaced view of SSM Parameter Store.
* :mod:`pdum.aws.quotas` — inspect Service Quotas and request increases.

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
"""

from pdum.aws.core import client, resource, session, whoami

__version__ = "0.4.0"

__all__ = [
    "__version__",
    "client",
    "resource",
    "session",
    "whoami",
]
