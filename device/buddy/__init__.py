# このリポジトリがデバイスへ載せるモジュール。flash では /flash/buddy/ に置き、
# upstream のピア (/flash/buddy_protocol.mpy など) と同じ階層に混ざらないようにする。
#
# 中身は持たせない。`from buddy import chat` を 1 つ引くだけでここも読み込まれるので、
# 置いたものはすべてのモジュールに付いて回るヒープになる。
