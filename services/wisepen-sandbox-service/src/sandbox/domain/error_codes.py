from common.core.domain import IErrorCode


class SandboxErrorCode(IErrorCode):

    POOL_EMPTY = (46001, "沙箱池中没有可用的 READY 容器")
    DOCKER_RUNTIME_FAILED = (46002, "docker 命令运行错误")
    WORKSPACE_PATH_INVALID = (46007, "工作区路径不合法")
    WORKSPACE_SYNC_FAILED = (46008, "沙箱工作区同步失败")
    WORKSPACE_CACHE_LIMIT_EXCEEDED = (46011, "沙箱工作区缓存超出限制")
    WORKSPACE_TRANSITION_IN_PROGRESS = (46012, "沙箱工作区正在切换，请稍后重试")
