"""Python 版重试钩子示例。

用法：复制成 gate.hooks.py 放在仓库根目录（闸门会自动找这个文件名），
或者在 gate.config.json 里写 "retry": {"hook": "我的钩子.py"} 指到别处。

每次上游返回限流状态码（默认 429/529）时，闸门会调一次 on_retry，
用返回值覆盖这一次的重试决策。改完文件保存即生效，不用重启闸门。

ctx 里有什么（和 Node 版 gate.hooks.example.js 完全一致，键名也一样）：
    status              上游状态码
    attempt             这是第几次发（首次 = 1）
    heldMs              这个请求到目前为止累计被暂存了多久
    retryAfterMs        上游 Retry-After 换算出的毫秒，没给就是 None
    method / path       请求方法和真实业务路径（不带 /__gate/u/<名字> 前缀）
    upstream            命名上游的名字，走默认上游时是 None
    window              请求头 x-gate-window 的值，用来区分是哪个客户端窗口
    stashed             当前正在暂存等待的请求数
    maxRetries / mode / cooldownMs / nextMinuteOffsetMs /
    respectRetryAfter / maxHoldMs    这一次原本要用的值（配置 + 请求头覆盖之后）

返回什么：
    None                不干预，按原本的值走
    False               这次不重试，把 429 原样透传给客户端
    dict                只有写了的键会被覆盖，其余照旧：
        maxRetries      这次最多重试几回（0 = 不重试）
        mode            "cooldown" | "nextMinute"
        cooldownMs      cooldown 模式等多久
        nextMinuteOffsetMs  nextMinute 模式对齐到第几毫秒（1000 = 第 01 秒）
        respectRetryAfter   是否听上游更久的 Retry-After
        maxHoldMs       这个请求的总暂存预算
        waitMs          直接指定这次等多久（给了它就不再算 mode / Retry-After）
        "retry": False  等同于返回 False

注意：
    - 钩子在处理该请求的线程里同步执行，别在里面做慢操作。
    - 钩子抛错或返回非法值只会记一条日志，然后按配置走，不影响请求。
    - 钩子改不了「哪些状态码算限流」，那个仍然由 retry.statuses 决定。
"""


def on_retry(ctx):
    # 例 1：真正的对话请求多扛几次，其他请求（比如 count_tokens）不值得等
    if ctx["path"] == "/v1/messages":
        # 已经等了超过 1 分钟就别再续了，把 429 交回客户端
        if ctx["heldMs"] > 60000:
            return False
        return {"maxRetries": 5}

    # 例 2：某个按整分钟重置配额的上游，用 nextMinute 模式等到下一分钟第 01 秒
    if ctx["upstream"] == "myprovider":
        return {"maxRetries": 3, "mode": "nextMinute", "nextMinuteOffsetMs": 1000}

    # 例 3：529（上游过载）比 429 更像临时抖动，短冷却快速重试
    if ctx["status"] == 529:
        return {"maxRetries": 4, "mode": "cooldown", "cooldownMs": 3000}

    # 其余不管，按 gate.config.json 的 retry.* 走
    return None
