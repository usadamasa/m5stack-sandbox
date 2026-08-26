"""Type stub for the PyPI `mpy-cross` package.

`py.typed` を持たず、stub パッケージも存在しない。ここに置くのは
`site-packages/mpy_cross/__init__.py` から写した、このリポジトリが触る面だけ。

`deploy_build.py` が使うのは `mpy_cross` 属性 1 つ。上流はバイナリのパスを
`glob(...)[0]` で決めるので `str` で、見つからなければ import した時点で
`SystemExit` になる (パッケージが壊れているとき以外は起きない)。
"""

# `run()` は使わない。上流の `subprocess.Popen` をそのまま返す口だが、
# `deploy_build.py` はタイムアウトを掛けたいので自前で `subprocess.run` を
# 呼んでいる。ここに書かないのは、書けば触っているように読めるため。
mpy_cross: str
