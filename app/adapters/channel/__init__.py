"""Channel adapters — one per supported channel.

Each adapter implements BaseChannelAdapter:
  - receive_message()      inbound: raw payload → UserAction
  - transform_activities() outbound: engine activities → channel-native wire format

Translation (inbound and outbound) is NOT done inside these adapters.
It is done upstream by the engine runner (engine/runner.py) so that the
engine always operates in English regardless of channel.

Available adapters:
  WebAdapter       — REST (Phase 1, current)
  WhatsAppAdapter  — Meta Cloud API (Phase 3 stub)
"""

from app.adapters.channel.base import BaseChannelAdapter
from app.adapters.channel.web import WebAdapter
from app.adapters.channel.whatsapp import WhatsAppAdapter

__all__ = ["BaseChannelAdapter", "WebAdapter", "WhatsAppAdapter"]
