"""ORM models for the master data and fulfillment facts shared by every domain.

These tables live unqualified in Postgres's default `public` schema rather
than a dedicated schema of their own.
"""

from app.models.common.carrier import Carrier
from app.models.common.delivery import Delivery, DeliveryLine, Shipment
from app.models.common.delivery_change_request import PoDeliveryChangeRequest
from app.models.common.demand_exception import DemandException
from app.models.common.location import RetailerLocation
from app.models.common.material import Material, MaterialMaster
from app.models.common.order_confirmation import OrderConfirmation, OrderConfirmationLine
from app.models.common.plant import Plant, StorageLocation, Warehouse
from app.models.common.production import ProductionOrder, ProductionSchedule
from app.models.common.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.common.retailer import Retailer
from app.models.common.retailer_agreement import RetailerAgreement
from app.models.common.sku import Sku

__all__ = [
    "Carrier",
    "Delivery",
    "DeliveryLine",
    "DemandException",
    "Material",
    "MaterialMaster",
    "OrderConfirmation",
    "OrderConfirmationLine",
    "Plant",
    "PoDeliveryChangeRequest",
    "ProductionOrder",
    "ProductionSchedule",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "Retailer",
    "RetailerAgreement",
    "RetailerLocation",
    "Shipment",
    "Sku",
    "StorageLocation",
    "Warehouse",
]
