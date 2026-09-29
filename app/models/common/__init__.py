"""ORM models for the master data and fulfillment facts shared by every domain.

These tables live unqualified in Postgres's default `public` schema rather
than a dedicated schema of their own.
"""

from mars_common.models.common import (
    Carrier,
    Delivery,
    DeliveryLine,
    DemandException,
    Material,
    MaterialMaster,
    OrderConfirmation,
    OrderConfirmationLine,
    Plant,
    PoDeliveryChangeRequest,
    ProductionOrder,
    ProductionSchedule,
    PurchaseOrder,
    PurchaseOrderLine,
    Retailer,
    RetailerAgreement,
    RetailerLocation,
    Shipment,
    Sku,
    StorageLocation,
    Warehouse,
)

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
