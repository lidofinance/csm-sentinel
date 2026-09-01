from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperatorAllocation:
    node_operator_id: int
    share: int
    effective_weight: int
    weighted_share: int


@dataclass(frozen=True, slots=True)
class OperatorGroupChange:
    node_operator_id: int
    node_operator_label: str
    old_allocation: OperatorAllocation | None = None
    new_allocation: OperatorAllocation | None = None

    def __post_init__(self) -> None:
        if self.old_allocation is None and self.new_allocation is None:
            raise ValueError("group change requires an old or new allocation")
