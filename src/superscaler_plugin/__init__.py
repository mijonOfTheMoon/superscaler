def make_superscaler_rpcinterface(supervisord, **config):
    from superscaler_plugin.rpcinterface import SuperscalerNamespaceRPCInterface
    return SuperscalerNamespaceRPCInterface(supervisord)