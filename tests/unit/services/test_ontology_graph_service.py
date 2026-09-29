from __future__ import annotations

import unittest
from uuid import uuid4

from rdflib import Graph

from app.models.cmir import CmirRecord
from app.models.common import Material, MaterialMaster
from app.services.ontology.graph_service import OntologyGraphService
from app.services.ontology.ontology_builder import OntologyBuilder


def _material(*, material_code: str) -> Material:
    return Material(id=uuid4(), material_code=material_code, description="test material")


def _material_master(
    *,
    material: Material,
    sap_material_number: str = "SAP-0001",
    discontinuation_indicator: str | None = None,
    follow_up_material: Material | None = None,
) -> MaterialMaster:
    return MaterialMaster(
        id=uuid4(),
        material_id=material.id,
        plant_id=None,
        sap_material_number=sap_material_number,
        discontinuation_indicator=discontinuation_indicator,
        follow_up_material_id=follow_up_material.id if follow_up_material else None,
    )


def _cmir_record(*, material_identity: str, customer_identity: str = "Acme Foods") -> CmirRecord:
    return CmirRecord(
        id=uuid4(),
        sender_type="external",
        customer_identity=customer_identity,
        material_identity=material_identity,
        existing_cmir_ref="",
        brand="AcmePlast",
        site="Site 12",
        # Not read by the builder for referencesMaterial matching (that's
        # material_identity, above) -- set to an unrelated placeholder only
        # because the model column is non-nullable.
        target_grd_code="UNUSED-BY-ONTOLOGY",
        target_customer_material_ref="ACM-4471",
        customer_identity_key=customer_identity.lower(),
        target_customer_material_ref_key="acm-4471",
    )


class OntologyGraphServiceTests(unittest.TestCase):
    def _build_graph(
        self,
        *,
        cmir_records=(),
        materials=(),
        material_masters=(),
    ) -> Graph:
        graph = Graph()
        OntologyBuilder().build_rdf_triples(
            graph,
            cmir_records=cmir_records,
            materials=materials,
            material_masters=material_masters,
            plants=[],
        )
        return graph

    def test_find_cmir_records_for_material_returns_every_referencing_record(self) -> None:
        material = _material(material_code="GRD-88213")
        record_a = _cmir_record(material_identity="GRD-88213", customer_identity="Acme Foods")
        record_b = _cmir_record(material_identity="GRD-88213", customer_identity="Nordic Retail")
        unrelated = _cmir_record(material_identity="GRD-OTHER", customer_identity="Other Co")
        other_material = _material(material_code="GRD-OTHER")

        graph = self._build_graph(
            cmir_records=[record_a, record_b, unrelated],
            materials=[material, other_material],
        )
        service = OntologyGraphService(graph=graph)

        result = service.find_cmir_records_for_material("GRD-88213")

        self.assertEqual(result.material_code, "GRD-88213")
        customer_identities = {r.customer_identity for r in result.cmir_records}
        self.assertEqual(customer_identities, {"Acme Foods", "Nordic Retail"})
        self.assertEqual(len(result.cmir_records), 2)

    def test_find_cmir_records_for_material_with_no_matches_returns_empty_list(self) -> None:
        graph = self._build_graph()
        service = OntologyGraphService(graph=graph)

        result = service.find_cmir_records_for_material("GRD-NONEXISTENT")

        self.assertEqual(result.cmir_records, [])

    def test_successor_chain_walks_multiple_hops_via_the_property_path(self) -> None:
        # discontinued -> mid (also discontinued) -> live (no further successor)
        # -- three materials, two hops, proving the traversal isn't stuck at
        # a single succeededBy assertion.
        discontinued = _material(material_code="GRD-OLD")
        mid = _material(material_code="GRD-MID")
        live = _material(material_code="GRD-LIVE")

        master_discontinued = _material_master(
            material=discontinued, discontinuation_indicator="Y", follow_up_material=mid
        )
        master_mid = _material_master(material=mid, discontinuation_indicator="Y", follow_up_material=live)
        master_live = _material_master(material=live)

        graph = self._build_graph(
            materials=[discontinued, mid, live],
            material_masters=[master_discontinued, master_mid, master_live],
        )
        service = OntologyGraphService(graph=graph)

        result = service.successor_chain(master_discontinued.id)

        self.assertEqual(str(master_discontinued.id), result.material_master_id)
        self.assertEqual([link.material_code for link in result.chain], ["GRD-MID", "GRD-LIVE"])
        self.assertEqual(result.chain[0].discontinuation_indicator, "Y")
        self.assertIsNone(result.chain[1].discontinuation_indicator)

    def test_successor_chain_for_a_material_with_no_successor_is_empty(self) -> None:
        material = _material(material_code="GRD-LIVE")
        master = _material_master(material=material)

        graph = self._build_graph(materials=[material], material_masters=[master])
        service = OntologyGraphService(graph=graph)

        result = service.successor_chain(master.id)

        self.assertEqual(result.chain, [])

    def test_successor_chain_stops_instead_of_looping_forever_on_a_cycle(self) -> None:
        # A malformed data fix could make two materials succeed each other.
        # The cycle guard must stop the walk instead of looping forever.
        material_a = _material(material_code="GRD-A")
        material_b = _material(material_code="GRD-B")
        master_a = _material_master(material=material_a, discontinuation_indicator="Y")
        master_b = _material_master(material=material_b, discontinuation_indicator="Y")
        # Wire the cycle after construction -- follow_up_material_id is set
        # at MaterialMaster-creation time above, so patch it in directly.
        master_a.follow_up_material_id = material_b.id
        master_b.follow_up_material_id = material_a.id

        graph = self._build_graph(
            materials=[material_a, material_b], material_masters=[master_a, master_b]
        )
        service = OntologyGraphService(graph=graph)

        result = service.successor_chain(master_a.id)

        # A -> B is one real hop; B -> A would repeat the starting node, so
        # the walk stops there instead of cycling forever.
        self.assertEqual([link.material_code for link in result.chain], ["GRD-B"])


if __name__ == "__main__":
    unittest.main()
