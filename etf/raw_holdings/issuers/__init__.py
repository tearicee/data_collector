"""各投信 adapter 註冊。

實作某家投信後，在此 import 其模組以觸發 @register，例如:
    from . import yuanta   # noqa: F401

已實作: 元大 (yuanta)、國泰 (cathay)、富邦 (fubon)、群益 (capital, playwright)、
中國信託 (ctbc, playwright)。其餘投信待逐家踩點，run_raw.py 會將尚無 adapter 的
代號歸入 pending_adapters.json，健檢摘要提示待補。
"""
from . import yuanta   # noqa: F401  觸發 @register("元大")
from . import cathay   # noqa: F401  觸發 @register("國泰")
from . import fubon    # noqa: F401  觸發 @register("富邦")
from . import capital  # noqa: F401  觸發 @register("群益")
from . import ctbc     # noqa: F401  觸發 @register("中國信託")
