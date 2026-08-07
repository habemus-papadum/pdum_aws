# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/habemus-papadum/pdum_aws/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                             |    Stmts |     Miss |   Cover |   Missing |
|--------------------------------- | -------: | -------: | ------: | --------: |
| src/pdum/aws/\_\_init\_\_.py     |        3 |        0 |    100% |           |
| src/pdum/aws/cli/\_\_init\_\_.py |       17 |        2 |     88% |   113-114 |
| src/pdum/aws/cli/\_\_main\_\_.py |        2 |        2 |      0% |       7-9 |
| src/pdum/aws/cli/dotenv.py       |       11 |        0 |    100% |           |
| src/pdum/aws/cli/identity.py     |       12 |        0 |    100% |           |
| src/pdum/aws/cli/output.py       |       16 |        0 |    100% |           |
| src/pdum/aws/cli/pdx.py          |      118 |        2 |     98% |   366-367 |
| src/pdum/aws/cli/quotas.py       |      118 |        4 |     97% |99, 101, 225-226 |
| src/pdum/aws/cli/secrets.py      |      125 |        2 |     98% |   223-224 |
| src/pdum/aws/core.py             |       11 |        0 |    100% |           |
| src/pdum/aws/quotas.py           |      100 |        1 |     99% |       213 |
| src/pdum/aws/secrets.py          |      108 |        1 |     99% |       300 |
| **TOTAL**                        |  **641** |   **14** | **98%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/habemus-papadum/pdum_aws/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/habemus-papadum/pdum_aws/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/habemus-papadum/pdum_aws/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/habemus-papadum/pdum_aws/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fhabemus-papadum%2Fpdum_aws%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/habemus-papadum/pdum_aws/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.