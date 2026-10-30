from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime


class MetricItem(BaseModel):
    id: str
    label: str
    value: str
    change_percentage: float
    is_positive: bool


class ActivityItem(BaseModel):
    id: str
    user: str
    action: str
    target: str
    timestamp: datetime
    status: str


class DashboardOverview(BaseModel):
    metrics: List[MetricItem]
    recent_activities: List[ActivityItem]
    system_status: str
