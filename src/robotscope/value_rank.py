"""单一职责：对记录做单一职责变换：value_rank。（作为流水线阶段）"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_value_rank", "ok")
    return rec
