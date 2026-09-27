from __future__ import annotations

import unittest
from uuid import uuid4

from rdflib import RDF, Graph, Literal

from app.models.cmir.cmir_record import CmirRecord
from app.models.common.material import Material, MaterialMaster
from app.models.common.plant import Plant
from app.services.ontology.ontology_builder import MARS, OntologyBuilder


def _material(*, material_code: str) -> Material:
    return Material(id=uuid4(), material_code=material_code, description="test material")


def _plant(*, plant_code: str) -> Plant:
    return Plant(id=uuid4(), plant_code=plant_code, plant_name="Test Plant")


def _material_master(
    *,
    material: Material,
    plant: Plant | None = None,
    sap_material_number: str = "SAP-0001",
    discontinuation_indicator: str | None = None,
    follow_up_material: Material | None = None,
) -> MaterialMaster:
    return MaterialMaster(
        id=uuid4(),
        material_id=material.id,
        plant_id=plant.id if plant else None,
        sap_material_number=sap_material_number,
        discontinuation_indicator=discontinuation_indicator,
        follow_up_material_id=follow_up_material.id if follow_up_material else None,
    )


def _cmir_record(*, brand: str, site: str, material_identity: str) -> CmirRecord:
    return CmirRecord(
        id=uuid4(),
        sender_type="external",
        customer_identity="Acme Foods",
        material_identity=material_identity,
        existing_cmir_ref="",
        brand=brand,
        site=site,
        # Not read by the builder for referencesMaterial matching (that's
        # material_identity, above) -- set to an unrelated placeholder only
        # because the model column is non-nullable.
        target_grd_code="UNUSED-BY-ONTOLOGY",
        target_customer_material_ref="ACM-4471",
        customer_identity_key="acme-foods",
        target_customer_material_ref_key="acm-4471",
    )


class OntologyBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = OntologyBuilder()
        self.graph = Graph()

    def test_material_is_typed_and_carries_its_code(self) -> None:
        material = _material(material_code="GRD-88213")

        self.builder.build_rdf_triples(
            self.graph, cmir_records=[], materials=[material], material_masters=[], plants=[]
        )

        iri = self.builder.material_iri("GRD-88213")
        self.assertIn((iri, RDF.type, MARS.Material), self.graph)
        self.assertIn((iri, MARS.materialCode, Literal("GRD-88213")), self.graph)

    def test_material_master_links_to_its_material_and_plant(self) -> None:
        material = _material(material_code="GRD-88213")
        plant = _plant(plant_code="PLANT-01")
        master = _material_master(material=material, plant=plant, sap_material_number="SAP-9001")

        self.builder.build_rdf_triples(
            self.graph,
            cmir_records=[],
            materials=[material],
            material_masters=[master],
            plants=[plant],
        )

        material_iri = self.builder.material_iri("GRD-88213")
        master_iri = self.builder.material_master_iri(master.id)
        plant_iri = self.builder.plant_iri("PLANT-01")

        self.assertIn((material_iri, MARS.hasPlantRecord, master_iri), self.graph)
        self.assertIn((master_iri, MARS.locatedAtPlant, plant_iri), self.graph)
        self.assertIn((master_iri, MARS.sapMaterialNumber, Literal("SAP-9001")), self.graph)
        # No discontinuation_indicator was set -- no triple should be asserted for it.
        self.assertNotIn((master_iri, MARS.discontinuationIndicator, None), self.graph)

    def test_discontinued_material_master_is_succeeded_by_its_replacement(self) -> None:
        discontinued = _material(material_code="GRD-88213")
        successor = _material(material_code="GRD-88510")
        master = _material_master(
            material=discontinued,
            discontinuation_indicator="Y",
            follow_up_material=successor,
        )

        self.builder.build_rdf_triples(
            self.graph,
            cmir_records=[],
            materials=[discontinued, successor],
            material_masters=[master],
            plants=[],
        )

        master_iri = self.builder.material_master_iri(master.id)
        successor_iri = self.builder.material_iri("GRD-88510")

        self.assertIn((master_iri, MARS.discontinuationIndicator, Literal("Y")), self.graph)
        self.assertIn((master_iri, MARS.succeededBy, successor_iri), self.graph)

    def test_cmir_record_references_the_material_its_identity_resolves_to(self) -> None:
        material = _material(material_code="GRD-88213")
        record = _cmir_record(brand="AcmePlast", site="Site 12", material_identity="GRD-88213")

        self.builder.build_rdf_triples(
            self.graph,
            cmir_records=[record],
            materials=[material],
            material_masters=[],
            plants=[],
        )

        record_iri = self.builder.cmir_record_iri(record.id)
        material_iri = self.builder.material_iri("GRD-88213")

        self.assertIn((record_iri, RDF.type, MARS.CmirRecord), self.graph)
        self.assertIn((record_iri, MARS.brand, Literal("AcmePlast")), self.graph)
        self.assertIn((record_iri, MARS.site, Literal("Site 12")), self.graph)
        self.assertIn((record_iri, MARS.customerIdentity, Literal("Acme Foods")), self.graph)
        self.assertIn(
            (record_iri, MARS.targetCustomerMaterialRef, Literal("ACM-4471")), self.graph
        )
        self.assertIn((record_iri, MARS.referencesMaterial, material_iri), self.graph)

    def test_cmir_record_with_unresolvable_identity_gets_no_reference_triple(self) -> None:
        # The bulk read didn't include a Material for this code -- e.g. it was
        # deleted, or the CMIR record is stale. The builder must not invent a
        # dangling reference to a Material node that was never asserted.
        record = _cmir_record(brand="AcmePlast", site="Site 12", material_identity="GRD-UNKNOWN")

        self.builder.build_rdf_triples(
            self.graph, cmir_records=[record], materials=[], material_masters=[], plants=[]
        )

        record_iri = self.builder.cmir_record_iri(record.id)
        self.assertNotIn((record_iri, MARS.referencesMaterial, None), self.graph)


if __name__ == "__main__":
    unittest.main()
