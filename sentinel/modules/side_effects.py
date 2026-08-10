from sentinel.models import Event
from sentinel.modules.community.adapter import CommunityModuleAdapter
from sentinel.modules.curated.adapter import CuratedModuleAdapter
from sentinel.modules.formatting import read_field


class NodeOperatorCountProcessor:
    def __init__(self, module_adapter) -> None:
        self.module_adapter = module_adapter

    async def process_event(self, event: Event) -> None:
        if event.event != "NodeOperatorAdded":
            return
        self.module_adapter.remember_node_operator_added(int(event.args["nodeOperatorId"]))


class CuratedMetadataCacheProcessor:
    def __init__(self, module_adapter: CuratedModuleAdapter) -> None:
        self.module_adapter = module_adapter

    async def process_event(self, event: Event) -> None:
        if event.event != "OperatorMetadataSet":
            return

        self.module_adapter.remember_node_operator_label(
            int(event.args["nodeOperatorId"]),
            read_field(event.args["metadata"], "name", 0) or None,
        )


EventSideEffectProcessor = NodeOperatorCountProcessor | CuratedMetadataCacheProcessor


class ModuleEventSideEffects:
    def __init__(self, module_adapter) -> None:
        self._processors = self._processors_for(module_adapter)

    @staticmethod
    def _processors_for(module_adapter) -> tuple[EventSideEffectProcessor, ...]:
        if isinstance(module_adapter, CommunityModuleAdapter):
            return (NodeOperatorCountProcessor(module_adapter),)

        if isinstance(module_adapter, CuratedModuleAdapter):
            return (
                CuratedMetadataCacheProcessor(module_adapter),
                NodeOperatorCountProcessor(module_adapter),
            )

        return ()

    async def process_event(self, event: Event) -> None:
        for processor in self._processors:
            await processor.process_event(event)
