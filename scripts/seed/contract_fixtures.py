"""Self-contained retailer agreement fixtures for contract intelligence and rule extraction.

These institutional-grade vendor agreements contain realistic legal clauses for OTIF on-time,
in-full shortage penalties, ASN timing fees, packaging specs, and dispute timelines.
"""

TARGET_CONTRACT_TEXT = """# MASTER VENDOR MERCHANDISE AND COMPLIANCE AGREEMENT

**THIS MASTER VENDOR MERCHANDISE AND COMPLIANCE AGREEMENT** (this “Agreement”) is entered into and made effective as of the 15th day of October, 2024 (the “Effective Date”), by and between **Target Corporation**, a Minnesota corporation having its principal corporate headquarters at 1000 Nicollet Mall, Minneapolis, Minnesota 55403, acting on behalf of itself and its operating retail affiliates, subsidiaries, and fulfillment divisions (collectively, “Target” or “Purchaser”), and **Mars Petcare US, Inc.**, a Delaware corporation having its principal place of business at 315 Cool Springs Boulevard, Franklin, Tennessee 37067, on behalf of itself and its corporate manufacturing affiliates (collectively, “Vendor” or “Supplier”). Target and Vendor are referred to individually as a “Party” and collectively as the “Parties.”

## RECITALS

**WHEREAS**, Target owns and operates a national omnichannel retail merchandising network throughout the United States, consisting of brick-and-mortar retail superstores, general merchandise stores, regional distribution centers (RDCs), food distribution centers (FDCs), cross-dock facilities, and e-commerce distribution platforms operating via Target.com;

**WHEREAS**, Vendor is a primary manufacturer and commercial packager of nationally branded companion animal care and pet nutrition consumables, including without limitation dog and cat food, veterinary-formulated diets, soft and hard treats, dental hygiene chews, and specialty pet supplies manufactured under prominent proprietary brand families including *Pedigree®*, *Iams®*, *Whiskas®*, *Greenies®*, and *Cesar®* (collectively, the “Products”);

**WHEREAS**, Target desires to procure Products from Vendor for distribution and resale throughout Target’s national retail supply chain network, and Vendor agrees to manufacture, package, sell, and deliver conforming Products in strict adherence to the terms, logistics guidelines, On-Time In-Full (OTIF) service level thresholds, packaging specifications, and legal conditions set forth in this Agreement and the incorporated Target Vendor Standards Manual;

**NOW, THEREFORE**, in consideration of the mutual covenants, promises, representations, warranties, and commercial undertakings contained herein, and for other good and valuable consideration, the receipt and sufficiency of which are hereby acknowledged, the Parties agree as follows:

---

## 1. DEFINITIONS AND INCORPORATION OF STANDARDS

**1.1 Defined Terms.** As used in this Agreement, the following capitalized terms shall have the respective meanings assigned below:
- **“Advance Shipping Notice”** or **“ASN”** means the Electronic Data Interchange (EDI) Transaction Set 856 containing detailed, accurate hierarchical carton-level and pallet-level shipping contents, purchase order numbers, SKU identifiers, and carrier shipment tracking numbers.
- **“Applicable Laws”** means all federal, state, and municipal statutes, codes, ordinances, regulations, and administrative orders applicable to the manufacture, safety, labeling, packaging, distribution, transportation, and sale of companion animal consumables, including the Federal Food, Drug, and Cosmetic Act (FD&C Act), the Food Safety Modernization Act (FSMA) and its implementing regulations (21 CFR Part 507), rules promulgated by the Association of American Feed Control Officials (AAFCO), the Fair Packaging and Labeling Act (FPLA), and California Proposition 65.
- **“Bill of Lading”** or **“BOL”** means the legal shipping document issued by Vendor or its designated carrier acknowledging receipt of the freight, establishing shipping terms, carrier pro-number, trailer seal number, piece count, pallet count, and delivery destination.
- **“Business Day”** means any calendar day other than a Saturday, Sunday, or official federal banking holiday in the United States.
- **“DDP”** means Delivered Duty Paid (Incoterms 2020) to Target’s designated destination Distribution Center or retail facility.
- **“Distribution Center”** or **“DC”** means any Target Regional Distribution Center (RDC), Food Distribution Center (FDC), Import Warehouse, or cross-dock facility identified on an applicable Purchase Order.
- **“EDI”** means Electronic Data Interchange operating under the American National Standards Institute (ANSI) Accredited Standards Committee (ASC) X12 protocol standard.
- **“GS1-128”** means the standardized linear barcode symbology (formerly UCC/EAN-128) utilized on master shipping containers and pallets encoding the Serial Shipping Container Code (SSCC-18).
- **“Must Arrive By Date”** or **“MABD”** means the mandatory calendar date or designated multi-day arrival window specified on the face of each Purchase Order during which freight must be physically delivered and dock-checked at the destination facility.
- **“OTIF”** means On-Time In-Full logistics performance evaluated against confirmed Purchase Order quantities and confirmed delivery windows.
- **“Purchase Order”** or **“PO”** means each individual electronic purchase order issued by Target to Vendor utilizing EDI Transaction Set 850 specifying ordered SKUs, unit quantities, negotiated pricing, shipping terms, destination DC, and MABD delivery window.
- **“Target Vendor Standards”** means Target’s comprehensive vendor compliance manual, logistics routing guide, packaging specifications, and business partner code of conduct, accessible via the Target Partners Portal, as updated from time to time.

**1.2 Order of Precedence.** In the event of an irreconcilable conflict between the body of this Agreement and any Purchase Order, Target Vendor Standards manual, or invoice, the terms of this Agreement shall control, followed in descending order by the specific terms of the Purchase Order, the Target Vendor Standards, and any written amendment executed by authorized corporate officers of both Parties.

---

## 2. PURCHASE ORDERS, EDI ACCEPTANCE, AND FORECASTING

**2.1 Purchase Order Issuance.** Target shall initiate all purchases of Products via electronic transmission of EDI 850 Purchase Orders. Each Purchase Order constitutes an offer by Target to purchase the specified quantities of Products under the prices, freight terms, and delivery schedules set forth therein.

**2.2 EDI Purchase Order Acknowledgment (EDI 855).** Vendor shall transmit an electronic Purchase Order Acknowledgment (EDI Transaction Set 855) or functional acknowledgment (EDI 997) within twenty-four (24) hours following Vendor’s receipt of each EDI 850. The EDI 855 must confirm Vendor’s acceptance of the PO, committed quantities per line item, and scheduled ship dates conforming to the designated MABD. If Vendor fails to transmit an explicit rejection of a Purchase Order within twenty-four (24) hours of receipt, the Purchase Order shall be deemed accepted in its entirety as issued. Any unilateral modification, conditional acceptance, or differing terms inserted by Vendor in an EDI 855 or invoice are expressly rejected and void *ab initio*.

**2.3 Order Modifications and Cancellations.** Target reserves the right to modify, reschedule, or cancel any unfulfilled Purchase Order, or portion thereof, without liability or restocking fee, upon providing electronic or written notice to Vendor at least five (5) Business Days prior to the scheduled origin shipping date.

**2.4 Non-Binding Forecasts.** Any demand projections, rolling volume forecasts, or historical sales trends supplied by Target to Vendor are non-binding estimates provided solely for manufacturing planning purposes. Nothing herein shall obligate Target to issue Purchase Orders for any minimum volume or dollar amount of Products.

---

## 3. LOGISTICS ROUTING, FREIGHT TERMS, AND MABD DELIVERY WINDOWS

**3.1 Freight and Title Terms.** Unless expressly designated otherwise on the applicable Purchase Order, all shipments under this Agreement shall move on a prepaid freight basis under Delivered Duty Paid (DDP - Incoterms 2020) terms to Target’s designated receiving facility dock. Vendor shall bear full operational responsibility, carrier freight costs, insurance, and risk of loss or transit damage until Products have been physically offloaded onto Target’s dock and Target has executed an electronic receipt scan.

**3.2 Must Arrive By Date (MABD) Compliance Window.** Target operates a precision-scheduled cross-dock and flow-through distribution network. Every Purchase Order carries a strict Must Arrive By Date (MABD) delivery window:
- For dry pet food, wet food, and treat merchandise shipped via prepaid truckload (TL) or less-than-truckload (LTL) freight, the MABD delivery window shall consist of a two (2) calendar day window ending on the designated MABD date, unless designated on the PO as a single, fixed-calendar-day appointment.
- Carrier delivery appointments must be booked through the Target Carrier Portal / Transportation Management System (TMS) at least seventy-two (72) hours prior to the commencement of the MABD window. Deliveries arriving prior to the opening of the MABD window or following the expiration of the MABD window are non-compliant.

---

## 4. ON-TIME IN-FULL (OTIF) PERFORMANCE, COMPLIANCE PENALTIES, AND LOGISTICS STANDARDS

**4.1 On-Time Delivery Compliance Benchmark.** Vendor covenants to maintain a monthly On-Time delivery compliance score of not less than ninety-five percent (95.0%) across all prepaid freight shipments delivered to Target facilities. On-Time performance is calculated by dividing the total number of conforming shipments arriving within the designated MABD window by the total number of shipments scheduled during the applicable calendar month.

**4.2 Late Arrival Compliance Penalty.** In the event Vendor fails to deliver conforming Products within the designated MABD delivery window established pursuant to Section 3.2, or Vendor’s monthly aggregate On-Time delivery compliance rate falls below the ninety-five percent (95.0%) threshold, Target shall assess, and Vendor shall be obligated to pay via invoice deduction, an On-Time compliance penalty equal to five percent (5.0%) of the gross invoice value of all delayed merchandise. The Parties agree that late arrivals cause dock congestion, scheduled labor under-utilization, and out-of-stock retail disruptions, and that the 5.0% assessment represents a reasonable pre-estimate of Target’s administrative damages and operational overhead.

**4.3 In-Full Fulfillment Compliance Benchmark.** Vendor covenants to maintain a monthly In-Full fulfillment rate of not less than ninety-five percent (95.0%) on each individual Purchase Order line item. The In-Full percentage is calculated as the total quantity of conforming saleable product units physically received at Target’s dock divided by the total number of units ordered on the applicable Purchase Order line.

**4.4 Purchase Order Shortage Penalty.** Where the quantity of conforming Products delivered on any individual Purchase Order line falls below ninety-five percent (95.0%) of the ordered unit quantity, Target shall assess, and Vendor shall pay via invoice deduction, a Shortage Non-Compliance Fee equal to three percent (3.0%) of the total purchase order invoice value of the unfulfilled (shorted) merchandise. Products omitted from delivery shall not be backordered; Target reserves the right to automatically cancel the unfulfilled quantity without further liability.

**4.5 Advance Shipping Notice (EDI 856 ASN) Requirements and Non-Compliance Fee.**
- Vendor must generate and transmit an accurate EDI 856 ASN to Target prior to the physical departure of the carrier's conveyance from Vendor’s manufacturing or shipping facility. The ASN must precisely mirror the physical pallet manifest, carton barcode serial numbers, and line-item SKU allocations contained on the trailer.
- If a carrier arrives at a Target facility without an ASN having been fully received, syntactically parsed, and accepted in Target's warehouse management system, or if the ASN is transmitted subsequent to carrier dispatch from the origin facility, Target shall assess an administrative ASN non-compliance chargeback of two hundred fifty dollars ($250.00) per Bill of Lading (BOL).

**4.6 Barcode Integrity and Carton Labeling Specifications.**
- Every master shipping container, case, and display carton delivered to Target must bear a clearly scannable GS1-128 linear barcode shipping label conforming to ANSI/ISO Print Quality Specification Grade C (1.5 on a 4.0 scale) or better. Barcodes must adhere strictly to GS1 General Specifications regarding quiet zones, barcode height, optical reflectivity, and SSCC format.
- Any shipment containing master cartons with unreadable, defaced, missing, or substandard barcodes requiring manual intake intervention, hand-keying, or physical relabeling by Target distribution center personnel shall incur an operational carton relabeling chargeback of thirty-five cents ($0.35) per non-scannable carton.

**4.7 Pallet Specifications and Unit Load Restacking Fee.**
- All shipments must be palletized on standard Grocery Manufacturers Association (GMA) Grade A 48-inch by 40-inch four-way hardwood pallets. Pallets must be structurally sound, free from broken top or bottom deck boards, protruding fasteners, rot, chemical contamination, or insect infestation.
- Unit loads must be securely stretch-wrapped and banded. In no event shall carton overhang exceed zero inches (0.0 in) beyond the 48x40-inch pallet perimeter, and total pallet height shall not exceed fifty-four (54) inches (including the pallet base) unless authorized in writing for specific high-density wet pet food SKUs.
- Any pallet exhibiting excessive overhang, leaning exceeding three inches, broken deck boards, structural instability, or shifting in transit requiring Target dock personnel to break down, re-palletize, or restack the load onto conforming pallets shall incur a pallet non-compliance fee of fifty dollars ($50.00) per restacked pallet.

---

## 5. COMMERCIAL TERMS, INVOICING, CASH DISCOUNTS, AND DEDUCTIONS

**5.1 Purchase Price.** The prices payable by Target for Products shall be the firm, net wholesale unit prices set forth in the applicable Purchase Order. Prices are fully inclusive of all manufacturing, primary and secondary packaging, master cartoning, palletization, shrink-wrapping, ocean/drayage/inland transportation, dunnage, fuel surcharges, export/import duties, and applicable taxes. Vendor warrants that prices may not be increased without ninety (90) days prior written notice and mutual written execution of an updated Pricing Addendum.

**5.2 Invoicing via EDI 810.** Invoices shall be submitted electronically via EDI Transaction Set 810 immediately following carrier departure from Vendor’s shipping origin. Invoices must accurately match the corresponding Purchase Order number, SKU line numbers, unit prices, and quantities acknowledged on the ASN.

**5.3 Payment Terms and Prompt Payment Cash Discount.** Standard payment terms are Net sixty (60) days from the later of (a) the date a complete and valid EDI 810 invoice is received by Target’s accounts payable system, or (b) the date of physical receipt and acceptance of conforming Products at Target’s destination facility. Target shall be entitled to deduct a prompt payment cash discount of two percent (2.0%) from the gross invoice amount if Target initiates electronic payment within fifteen (15) calendar days following receipt of the invoice (“2% 15, Net 60”).

**5.4 Defective Merchandise and Swell Allowance.** In lieu of Target physically returning individual unsaleable, crushed, torn, dented, or superficially damaged pet food cartons, wet food cans, or treat pouches encountered during retail handling, Vendor grants to Target an off-invoice Swell and Damage Allowance equal to one and one-half percent (1.5%) of the total gross invoice value on all Purchase Orders. Target shall automatically deduct this 1.5% allowance from each invoice remittance. The Swell Allowance does not waive or impair Target’s right to pursue separate recovery for latent manufacturing defects, systemic spoilage, or product recalls under Section 6.

**5.5 Right of Set-Off and Administrative Deductions.** Target shall have the contractual right at all times to set off and deduct any amounts owed by Vendor to Target—including OTIF penalties, late arrival fees, line shortage assessments, ASN fines, barcode relabeling fees, pallet restacking fees, swell allowances, and indemnification liabilities—against any current or future invoice payments owed by Target to Vendor under this Agreement or any other agreement between the Parties.

---

## 6. INSPECTION, QUALITY ASSURANCE, AND PRODUCT RECALLS

**6.1 Receipt and Non-Conforming Goods.** Target reserves the right to inspect and audit Products upon arrival at Target DCs or retail store docks. Neither physical unloading, execution of a carrier delivery receipt, nor payment of an invoice shall constitute final acceptance of defective, expired, damaged, or non-conforming merchandise. Target may, at its sole election and at Vendor’s sole expense, reject, hold for disposition, or return any shipment or lot of Products that fails to conform to approved specifications, contains foreign contaminants, exhibits burst pouch seals or dented cans, or violates Applicable Laws.

**6.2 Product Recall and Safety Alert Obligations.**
- Vendor shall notify Target in writing and by telephone within twelve (12) hours of discovering, receiving regulatory inquiry regarding, or suspecting any contamination, adulteration, foreign material presence, salmonella or listeria contamination, misbranding, undeclared allergen, or defect in any Product that could trigger a voluntary or mandatory Class I, II, or III recall, market withdrawal, or regulatory alert (a “Recall”).
- In the event of any Recall involving Vendor’s Products, Vendor shall bear full operational and financial responsibility for all direct, incidental, and consequential costs incurred by Target, including without limitation: reverse transportation, cross-dock handling, product isolation, certified destruction, regulatory reporting, communications with retail guests, customer refunds, and store-level labor for removing Products from store shelves.
- In addition to reimbursing Target for all actual recall costs and lost retail margins, Vendor shall pay to Target a fixed, liquidated Recall Administrative Coordination Fee of fifty thousand dollars ($50,000.00) per distinct Recall event. The Parties stipulate and agree that this administrative fee represents a fair and reasonable approximation of Target’s corporate crisis management overhead, inventory software reconfiguration, and brand mitigation expenses, and does not constitute a penalty.

---

## 7. DISPUTE RESOLUTION, AUDIT RIGHTS, AND CHARGEBACK RECONCILIATION

**7.1 Mandatory 30-Day Dispute Window.** In the event Vendor disputes any operational compliance deduction, OTIF late fee, line shortage penalty, ASN chargeback, or relabeling assessment deducted by Target pursuant to Section 4, Vendor must submit a formal electronic dispute notice through the Target Partners AP Portal within thirty (30) calendar days following the date of the deduction remittance advice. Any chargeback or deduction not formally contested with complete supporting documentation within said thirty (30) day window shall be deemed permanently waived, final, and uncontestable by Vendor.

**7.2 Evidentiary Burden and Telematics Proof Requirements.** To sustain a valid dispute against an On-Time delivery or appointment compliance deduction, Vendor must provide objective, third-party electronic evidence establishing carrier compliance, specifically:
- A fully executed electronic or physical Bill of Lading bearing Target’s authorized receiving dock stamp, date, and verifiable time-in/time-out notation;
- Electronic Data Interchange shipment status transaction records (EDI 214) confirming delivery within the designated MABD window; and
- Unaltered satellite telematics data, electronic logging device (ELD) records, and carrier GPS breadcrumb logs corroborating that the carrier conveyance arrived at the designated Target facility security gate within the scheduled appointment arrival window.
Carrier self-generated dispatch sheets, manual driver affidavits, or internal Vendor transit logs lacking independent time verification are legally inadmissible to substantiate a dispute.

**7.3 Target 60-Day Reconciliation Review.** Target’s Vendor Reconciliation Group shall review each timely submitted dispute and provide an electronic determination within sixty (60) calendar days following complete evidentiary submission. If Target validates Vendor’s claim that a penalty resulted from Target-caused dock delays, facility gate lockouts, or confirmed Target IT transmission errors, Target shall credit the disputed amount back to Vendor’s account on the subsequent bi-monthly remittance cycle.

---

## 8. REPRESENTATIONS, WARRANTIES, AND INDEMNIFICATION

**8.1 Vendor Warranties.** Vendor represents, warrants, and covenants to Target that:
- All Products delivered hereunder shall be merchantable, safe, pure, unadulterated, wholesome, fit for the nutritional feeding of companion animals, and strictly compliant with all Specifications, approved samples, and Applicable Laws;
- No Product shall, as of the delivery date, be adulterated or misbranded within the meaning of the FD&C Act or FSMA, nor contain levels of heavy metals, pesticides, mycotoxins (including aflatoxin), or microbial pathogens exceeding allowable federal and state thresholds;
- Vendor holds good and marketable title to all Products free and clear of all liens, claims, or third-party encumbrances; and
- The Products, primary packaging, graphic designs, and associated trademarks (*Pedigree*, *Iams*, *Whiskas*, *Greenies*, *Cesar*) do not and shall not infringe upon or misappropriate any patent, trademark, copyright, trade dress, or proprietary right of any third party.

**8.2 Indemnification.** Vendor shall defend, indemnify, and hold harmless Target Corporation, its corporate parent, subsidiaries, affiliates, retail divisions, officers, directors, employees, and retail guests from and against any and all third-party claims, lawsuits, administrative actions, regulatory fines, losses, liabilities, settlements, judgments, and legal expenses (including reasonable attorneys' fees and expert costs) arising out of or resulting from: (a) actual or alleged injury, illness, or death to any domestic animal or human caused by consumption or handling of the Products; (b) any defect in design, manufacture, packaging, or warning labels; (c) any violation of Applicable Laws; (d) any intellectual property infringement claim; or (e) any negligent act, omission, or willful misconduct by Vendor, its employees, or logistics agents.

**8.3 Insurance Requirements.** Vendor shall maintain at its sole expense: (a) Commercial General Liability insurance, including Broad Form Contractual Liability and Products/Completed Operations coverage, with limits of not less than five million dollars ($5,000,000.00) per occurrence and ten million dollars ($10,000,000.00) in the aggregate; and (b) Statutory Workers’ Compensation and Employer’s Liability insurance. Target Corporation shall be endorsed as an Additional Insured on all liability policies, and Vendor shall furnish certificates of insurance evidencing such coverage prior to shipping Products.

---

## 9. FORCE MAJEURE

**9.1 Qualifying Events.** Neither Party shall be liable for failure to perform its delivery or receipt obligations if such failure arises from an unforeseeable event beyond the reasonable control of the affected Party, without fault or negligence, limited strictly to: acts of God, extreme and documented severe weather catastrophes (such as tornadoes, hurricanes, or catastrophic regional blizzards directly shutting transit corridors), official Department of Transportation (DOT) interstate highway closures documented by state highway patrol authorities, armed conflict, declared epidemics resulting in governmental facility shutdowns, or lawful industry-wide rail or port labor strikes (“Force Majeure Event”).

**9.2 Exclusions.** Force Majeure shall specifically exclude: general market raw material shortages, packaging component delays, price fluctuations, routine mechanical breakdowns, driver shortages, or carrier freight rate negotiations.

**9.3 Notice and Mitigation.** The affected Party must give written notice to the other Party within twenty-four (24) hours of the onset of the Force Majeure Event, accompanied by official third-party documentary proof (e.g., DOT road closure notices or meteorological alerts), and must exercise diligent commercial efforts to mitigate the delay, re-route shipments, or utilize alternative distribution centers.

---

## 10. TERM, TERMINATION, AND GOVERNING LAW

**10.1 Term.** This Agreement shall commence on the Effective Date and shall continue in full force and effect for an initial term of two (2) calendar years, automatically renewing for successive one (1) year terms unless either Party provides written notice of non-renewal at least ninety (90) calendar days prior to the expiration of the then-current term.

**10.2 Termination for Cause.** Either Party may terminate this Agreement immediately upon written notice if the other Party: (a) materially breaches any provision of this Agreement and fails to cure such breach within thirty (30) days following receipt of written notice; (b) initiates or becomes subject to bankruptcy, receivership, or insolvency proceedings; or (c) in the case of Vendor, delivers adulterated Products that prompt a Class I Recall or regulatory seizure.

**10.3 Governing Law and Venue.** This Agreement, and all disputes, claims, or controversies arising out of or relating to its execution, performance, or interpretation, shall be governed by, construed, and enforced in accordance with the internal substantive laws of the **State of Minnesota**, without giving effect to its conflict of law principles. The Parties irrevocably consent and submit to the exclusive personal and subject matter jurisdiction of the United States District Court for the District of Minnesota (Minneapolis Division) or the State District Court for Hennepin County, Minnesota.

**10.4 Severability and Integration.** If any provision of this Agreement is held to be invalid or unenforceable, such determination shall not affect the validity of the remaining provisions. This Agreement, together with the incorporated Target Vendor Standards, constitutes the entire agreement between the Parties with respect to the subject matter hereof and supersedes all prior proposals, negotiations, and understandings.

---

**IN WITNESS WHEREOF**, the Parties hereto have executed this Master Vendor Merchandise and Compliance Agreement by their duly authorized corporate officers as of the Effective Date.

**TARGET CORPORATION**

By: ___________________________________  
Name: Sarah Jenkins  
Title: Senior Vice President, Merchandising & Essentials  
Date: October 15, 2024  

**MARS PETCARE US, INC.**

By: ___________________________________  
Name: Marcus Vance  
Title: Vice President, Commercial Sales & Supply Chain  
Date: October 15, 2024  
"""

KROGER_CONTRACT_TEXT = """# MASTER LOGISTICS AND VENDOR COMPLIANCE AGREEMENT

**THIS MASTER LOGISTICS AND VENDOR COMPLIANCE AGREEMENT** (this “Agreement”) is entered into and made effective as of the 1st day of November, 2024 (the “Effective Date”), by and between **The Kroger Co.**, an Ohio corporation having its principal executive offices at 1014 Vine Street, Cincinnati, Ohio 45202, on behalf of itself and its retail divisions, banner store subsidiaries, distribution centers, and cross-dock fulfillment platforms (collectively, “Kroger” or the “Company”), and **Mars Petcare US, Inc.**, a Delaware corporation having its principal place of business at 315 Cool Springs Boulevard, Franklin, Tennessee 37067, on behalf of itself and its operating manufacturing subsidiaries (collectively, “Vendor” or “Supplier”). Kroger and Vendor may be referred to individually as a “Party” and collectively as the “Parties.”

## RECITALS

**WHEREAS**, Kroger operates a nationwide network of retail supermarkets, multi-department superstores, automated replenishment facilities, regional distribution centers (DCs), and local Direct Store Delivery (DSD) receiving portals operating across multiple corporate banner banners;

**WHEREAS**, Vendor manufactures, commercializes, packages, and markets premium companion animal nutritional products and treats, including dry kibble, wet canned food, hermetically sealed wet pet food pouches, and dental treats under prominent proprietary brands including *Pedigree®*, *Iams®*, *Whiskas®*, *Greenies®*, and *Cesar®* (collectively, the “Products”);

**WHEREAS**, Kroger desires to purchase Products from Vendor, and Vendor desires to sell and deliver Products to Kroger’s distribution centers and designated retail stores via both Distribution Center (DC) fulfillment and Direct Store Delivery (DSD) supply channels;

**WHEREAS**, Kroger’s automated distribution network requires rigorous inbound appointment adherence, high fill rate reliability, timely electronic data interchange (EDI) messaging, strict temperature and packaging integrity, and standardized dispute resolution protocols as set forth in this Agreement and the incorporated Kroger Vendor Logistics & Compliance Manual;

**NOW, THEREFORE**, in consideration of the mutual covenants, representations, warranties, and commercial agreements herein set forth, the Parties agree as follows:

---

## 1. DEFINITIONS AND INCORPORATED STANDARDS

**1.1 Defined Terms.** As used in this Agreement, the following terms shall have the respective meanings indicated:
- **“Appointment Window”** means the precise, mandatory one-hour (1-hour) on-dock delivery time window established in Kroger’s Transportation Management System (TMS), beginning thirty (30) minutes prior to the scheduled appointment time and ending thirty (30) minutes after the scheduled appointment time.
- **“Advance Shipping Notice”** or **“ASN”** means the electronic EDI Transaction Set 856 transmitted by Vendor detailing shipment structure, purchase order number, Bill of Lading number, carrier SCAC, trailer seal number, SKU-level item detail, and physical case counts.
- **“Bill of Lading”** or **“BOL”** means the official legal shipping document accompanying the freight containing the carrier pro number, purchase order numbers, pallet count, case count, and receiving endorsement spaces.
- **“Direct Store Delivery”** or **“DSD”** means the operational delivery model wherein Vendor or its authorized third-party distributor delivers Products directly to individual Kroger retail store backrooms rather than through a Kroger regional distribution center.
- **“Distribution Center”** or **“Kroger DC”** means any regional dry goods, ambient, or temperature-monitored grocery distribution facility operated by Kroger.
- **“EDI”** means Electronic Data Interchange operating under ANSI ASC X12 communication standards.
- **“FSMA”** means the FDA Food Safety Modernization Act (Public Law 111-353) and its implementing animal food safety regulations under 21 CFR Part 507.
- **“Manhattan TMS”** or **“One Network”** means the centralized web-based transportation scheduling and yard management systems utilized by Kroger to schedule inbound dock appointments and track carrier compliance.
- **“Purchase Order”** or **“PO”** means the electronic purchase order transmitted by Kroger to Vendor via EDI 850 specifying ordered SKUs, unit quantities, destination facility, and requested delivery dates.
- **“Purchase Order Fill Rate”** means the percentage calculated by dividing the total quantity of conforming product units physically received and accepted at Kroger facilities by the total quantity of product units ordered on the applicable Purchase Order.
- **“Receiver Stamp”** means the physical ink impression or electronic receipt stamp applied by an authorized Kroger DC receiving clerk or DSD store receiving manager to the delivery document, displaying the facility number, arrival timestamp, received piece count, and clerk initials.
- **“Return Handling Fee”** means the administrative and reverse logistics chargeback assessed by Kroger on rejected or non-conforming merchandise lots.

**1.2 Document Priority.** In the event of any conflict between this Agreement and any Purchase Order, shipment manifest, routing guide, or vendor portal communication, the explicit terms of this Agreement shall govern and prevail.

---

## 2. ORDER TRANSMISSION, EDI PROTOCOLS, AND SUPPLY CHANNELS

**2.1 Electronic Purchase Order Generation (EDI 850).** All procurement transactions shall be initiated through EDI 850 Purchase Orders. Each PO shall specify whether the fulfillment channel is designated as Distribution Center (DC) delivery or Direct Store Delivery (DSD).

**2.2 Purchase Order Acknowledgment (EDI 855).** Vendor shall electronically transmit an EDI 855 Purchase Order Acknowledgment within twenty-four (24) hours of receipt of each EDI 850. The acknowledgment must confirm full acceptance of ordered item quantities and commitment to the delivery schedule. Failure to transmit an explicit rejection within twenty-four (24) hours shall constitute irrevocable deemed acceptance of the Purchase Order as transmitted.

**2.3 Fulfillment Channels.**
- **DC Fulfillment:** Vendor shall ship consolidated truckload (TL) or less-than-truckload (LTL) shipments directly to designated Kroger regional DCs in accordance with appointment schedules established under Section 3.
- **DSD Fulfillment:** For designated specialty pet treats and perishable/short-shelf-life displays, Vendor shall deliver directly to specified retail store locations during authorized backroom receiving hours (6:00 AM to 1:00 PM local store time, Monday through Friday).

---

## 3. APPOINTMENT SCHEDULING AND CARRIER DOCK WINDOWS

**3.1 Mandatory TMS Appointment Scheduling.** All inbound freight destined for a Kroger DC—whether moving on a Vendor-prepaid basis or Kroger-managed freight—must have a confirmed delivery appointment booked through Kroger’s mandated logistics platform (One Network Enterprises or Manhattan Associates TMS). Inbound carrier appointments must be scheduled at least forty-eight (48) hours prior to arrival.

**3.2 One-Hour Appointment Delivery Window.** Kroger facilities operate under tightly choreographed dock schedules. Carriers are required to arrive at the facility security gate and check in within the designated **one-hour (1-hour) Appointment Window** (specifically, no earlier than thirty [30] minutes prior to the scheduled appointment time and no later than thirty [30] minutes following the scheduled appointment time).

**3.3 Unscheduled or Out-of-Window Deliveries.** Shipments arriving outside the designated Appointment Window, or arriving without a confirmed appointment in One Network / Manhattan TMS, shall be deemed non-compliant and may be refused at the gate, held in the carrier staging yard at Vendor’s expense, or worked in solely at Kroger’s operational discretion as dock capacity permits.

---

## 4. LOGISTICS PERFORMANCE, LATENESS CHARGES, AND SHORTAGE PENALTIES

**4.1 Late Delivery Appointment Penalty.** In the event a carrier conveying Vendor’s Products arrives at the Kroger DC gate outside the confirmed one-hour Appointment Window established pursuant to Section 3.2 without having obtained an approved appointment rescheduling in One Network or Manhattan TMS at least twenty-four (24) hours prior to the original appointment time, Vendor shall be assessed, and Kroger shall deduct from invoice payment, a flat administrative late appointment penalty of **five hundred dollars ($500.00)** per late truckload appointment.

**4.2 Rescheduled Delivery Fee (>24 Hours).** If an out-of-window or rejected delivery cannot be worked into the DC dock schedule on the original arrival date and must be rescheduled for an on-dock receiving appointment more than twenty-four (24) hours following the original scheduled appointment date, an additional inventory disruption and holding charge of **fifty dollars ($50.00) per pallet** shall be assessed against the shipment in addition to the flat late fee set forth in Section 4.1.

**4.3 Purchase Order Fill Rate Compliance Benchmark.** Vendor covenants to maintain an aggregate Purchase Order Fill Rate of not less than **ninety-seven percent (97.0%)** across all dry and wet pet nutrition SKUs, measured across all Purchase Orders issued during each rolling monthly billing cycle.

**4.4 Purchase Order Shortage Penalty.** If Vendor’s order fulfillment on any individual Purchase Order falls below the mandatory ninety-seven percent (97.0%) fill rate benchmark, Kroger shall assess, and automatically deduct from remittance, a Purchase Order Shortage Chargeback equal to **four percent (4.0%)** of the total gross dollar amount of the unfulfilled (shorted) purchase order quantity. Kroger shall not accept unauthorized backorders; unfulfilled quantities shall be automatically cancelled upon receipt of the partial shipment.

**4.5 Advance Shipping Notice (EDI 856 ASN) Compliance.**
- Vendor must transmit a complete, syntactically conforming ANSI X12 EDI 856 ASN transaction that is successfully received and processed by Kroger’s Warehouse Management System (WMS) at least **two (2) hours prior** to the carrier’s scheduled on-dock appointment.
- Any shipment arriving at a Kroger facility where the ASN is missing, unreadable, delayed past the two-hour pre-arrival threshold, or materially discrepant from the physical BOL pallet and case manifest, shall incur an administrative chargeback of **one hundred fifty dollars ($150.00)** per occurrence.

---

## 5. TEMPERATURE, PACKAGING, AND MOISTURE INTEGRITY (WET FOOD AND POUCHES)

**5.1 Shelf-Stable Wet Nutrition and Pouch Integrity Standards.** Vendor manufactures and supplies wet canned pet food (incorporating pull-tab / easy-open aluminum and steel ends) and hermetically sealed retort flexible pouches (including *Cesar®* loaf tubs and *Whiskas®* pouches). Vendor warrants that all primary containers are hermetically sealed, commercially sterile, and free from micro-punctures, pinholes, delamination, or defective crimping.

**5.2 Temperature Compliance in Transit.** During all transit and staging stages, Products must be maintained within approved environmental temperature ranges:
- Shelf-stable wet food cans, tubs, and pouches must not be exposed to internal trailer temperatures exceeding ninety degrees Fahrenheit (90°F / 32°C), which could accelerate organoleptic degradation or chemical can lining breakdown; and
- Products must be protected against freezing conditions (below 32°F / 0°C) that could cause expansion, compromise lid hermetic seals, or cause pouch seam bursting.

**5.3 Defective Lot Rejection.** Kroger receiving personnel shall conduct visual and physical inbound inspections. Kroger reserves the right to reject an entire inbound lot or full truckload shipment if:
- Inbound inspection reveals evidence of blown lids, leaking wet pouches, sweller cans, rusted chimes, unsealed foil lids, or pest/insect activity exceeding a zero-tolerance threshold; or
- Transit temperature recording devices (temp-tales) or trailer reefer telematics demonstrate unauthorized temperature excursions outside the ranges specified in Section 5.2.

**5.4 Return Processing and Handling Fee.** For any shipment or lot rejected pursuant to Section 5.3 that is returned to Vendor or directed to certified destruction at Kroger’s facility, Vendor shall be liable for all actual freight and landfill/destruction expenses, and in addition, Kroger shall assess an administrative **Return Handling Fee equal to eight percent (8.0%)** of the total gross purchase order wholesale value of the rejected lot. Vendor acknowledges that handling contaminated, leaking, or damaged wet pet food requires specialized hazardous waste sanitation, dock cleanup, and disposal protocols justifying said 8.0% chargeback.

---

## 6. COMMERCIAL INVOICING, CREDIT TERMS, AND DIRECT STORE DELIVERY

**6.1 Invoicing via EDI 810.** Invoices shall be submitted electronically via EDI 810 following freight departure. Invoices must reference the corresponding Kroger PO number, destination facility code, validated line quantities, and negotiated unit prices.

**6.2 Payment Terms.** Undisputed invoices shall be paid Net sixty (60) days following the date of complete physical receipt and electronic acceptance of conforming Products at the destination DC or retail store.

**6.3 DSD Receiving and Scan-Back Verification.** For all Direct Store Delivery (DSD) orders:
- Deliveries must be checked in by an authorized Kroger store receiving clerk utilizing Kroger’s handheld Electronic Data Entry (DEX) / Direct Store Delivery terminal;
- The delivery ticket must be electronically signed and counter-stamped with the official store Receiver Stamp; and
- Vendor’s delivery driver must leave an exact duplicate physical delivery invoice with the receiving clerk. Any discrepancy between invoiced quantities and physical DEX scan-in quantities shall be deducted automatically from the invoice voucher.

**6.4 Automated Set-Off and Deduction.** Kroger shall be entitled to deduct and set off any accrued late delivery fees, pallet rescheduling fees, order shortage chargebacks, ASN fines, return handling fees, and indemnity obligations directly against any current or future accounts payable remittances due to Vendor.

---

## 7. CHARGEBACK DISPUTE PROCEDURE AND AUDIT RECONCILIATION

**7.1 Mandatory 45-Day Dispute Window.** If Vendor disputes any operational compliance deduction, late appointment charge, fill rate penalty, ASN chargeback, or return handling fee deducted by Kroger, Vendor must initiate a formal claim through the **Kroger Vendor Portal (Kroger AP / Chargeback Dispute Portal)** within **forty-five (45) calendar days** following the date of the remittance advice indicating the deduction. Any dispute not formally submitted within said forty-five (45) day window shall be permanently time-barred, forfeited, and deemed accepted by Vendor.

**7.2 Mandatory Documentary Evidence.** To substantiate any dispute regarding on-dock appointment timing, delivery quantities, or ASN transmission, Vendor must upload the following mandatory documents into the Kroger Vendor Portal:
- A legible, unaltered copy of the signed Bill of Lading (BOL) or store delivery ticket bearing the physical or electronic Kroger **Receiver Stamp**, legible receiving clerk signature, date, and verifiable gate-in / dock arrival timestamp;
- Certified carrier GPS telematics data and electronic logging device (ELD) timestamp records establishing carrier arrival at the Kroger facility security gate within the confirmed one-hour Appointment Window;
- EDI 997 Functional Acknowledgment demonstrating that the EDI 856 ASN was successfully accepted by Kroger’s EDI gateway at least two (2) hours prior to dock arrival; and
- In the case of fill rate disputes, documented evidence of prior written cancellation or allocation approval issued by Kroger’s corporate category procurement team.
Claims supported solely by internal Vendor ERP logs, driver written statements, or dispatch printouts without official Kroger receiving stamps shall be rejected summarily.

**7.3 Reconciliation Timelines.** Kroger Vendor Accounting shall investigate complete claims and issue a written determination via the Vendor Portal within **sixty (60) calendar days** of formal claim submission. If Kroger determines that the deduction was issued in error due to internal Kroger dock delays, gate queue backups exceeding ninety (90) minutes, or WMS processing delays, Kroger shall credit the full disputed amount back to Vendor via Electronic Funds Transfer (EFT) within two (2) subsequent payment cycles.

---

## 8. FOOD SAFETY (FSMA), QUALITY, AND REGULATORY COMPLIANCE

**8.1 FSMA Compliance.** Vendor warrants and covenants that all Products supplied under this Agreement are manufactured, processed, packed, and held in strict compliance with the FDA Food Safety Modernization Act (FSMA) Preventive Controls for Animal Food rule (21 CFR Part 507) and Current Good Manufacturing Practice (CGMP) guidelines. Vendor maintains a validated, written Food Safety Plan, including comprehensive hazard analyses, preventive controls, supply chain programs, and sanitation controls.

**8.2 Pathogen Surveillance and Microbial Standards.** Vendor shall maintain robust environmental monitoring and finished product testing protocols for *Salmonella spp.*, *Listeria monocytogenes*, enterobacteria, and mycotoxins. Finished pet food lots exhibiting positive pathogen findings shall not be distributed to Kroger under any circumstances.

**8.3 Audit Rights.** Kroger food safety quality assurance auditors shall have the contractual right, upon forty-eight (48) hours advance written notice, to enter and inspect Vendor’s manufacturing and distribution facilities during normal business hours to verify FSMA compliance, sanitation records, pest control logs, and third-party Global Food Safety Initiative (GFSI) audit certifications (e.g., SQF or BRC).

---

## 9. WARRANTIES, INDEMNIFICATION, AND INSURANCE

**9.1 Vendor Warranties.** Vendor warrants that: (a) all Products delivered hereunder shall be wholesome, merchantable, pure, unadulterated, and safe for pet consumption; (b) all Products shall carry a remaining code-dated shelf life of not less than seventy-five percent (75.0%) of total manufacturer shelf life upon delivery to Kroger DCs; (c) the Products and their packaging comply fully with AAFCO nutritional profiles and all Applicable Laws; and (d) the Products do not infringe upon any third-party intellectual property rights.

**9.2 Indemnification.** Vendor shall indemnify, defend, and hold harmless The Kroger Co., its affiliates, retail divisions, subsidiaries, directors, officers, employees, and agents from and against any and all third-party claims, lawsuits, administrative demands, losses, damages, liabilities, regulatory fines, and legal expenses (including reasonable attorneys' fees) arising out of or resulting from: (a) animal illness, injury, or death or human illness caused by ingestion or handling of the Products; (b) packaging defects, structural seal failures, or foreign material contamination; (c) any product recall initiated by Vendor or mandated by the FDA; or (d) any breach of Vendor’s warranties or representations.

**9.3 Insurance.** Vendor shall carry and maintain: (a) Commercial General Liability insurance, including Products/Completed Operations coverage, with limits of at least five million dollars ($5,000,000.00) per occurrence and ten million dollars ($10,000,000.00) aggregate; and (b) Umbrella/Excess Liability coverage of not less than ten million dollars ($10,000,000.00). The Kroger Co. shall be endorsed as an Additional Insured on all primary and excess liability policies.

---

## 10. TERM, TERMINATION, AND GOVERNING LAW

**10.1 Term.** This Agreement shall be effective for an initial term of two (2) years commencing on the Effective Date and shall automatically renew for successive one (1) year periods unless either Party provides written notice of termination to the other Party at least sixty (60) days prior to the expiration of the then-current term.

**10.2 Termination for Cause.** Either Party may terminate this Agreement immediately upon written notice if the other Party materially breaches any obligation hereunder and fails to cure such breach within thirty (30) calendar days after receipt of written default notice; provided, however, that Kroger may terminate this Agreement immediately without cure opportunity if Vendor delivers contaminated or adulterated Products that prompt an FDA safety warning or Class I Recall.

**10.3 Governing Law and Exclusive Venue.** This Agreement, and all claims or controversies arising out of or related hereto, shall be governed by and construed in accordance with the substantive laws of the **State of Ohio**, without giving effect to any choice of law or conflict of law rules that would result in the application of the laws of any other jurisdiction. The Parties submit to the exclusive personal jurisdiction and venue of the state and federal courts located in **Hamilton County, Cincinnati, Ohio**.

**10.4 Entire Agreement.** This Agreement, together with all active Purchase Orders and the Kroger Vendor Logistics & Compliance Manual, represents the complete understanding between the Parties, superseding all prior oral or written agreements. No amendment, modification, or waiver shall be valid unless in writing and signed by authorized executive officers of both Kroger and Vendor.

---

**IN WITNESS WHEREOF**, the Parties hereto have executed this Master Logistics and Vendor Compliance Agreement as of the Effective Date written above.

**THE KROGER CO.**

By: ___________________________________  
Name: David M. Cunningham  
Title: Group Vice President, Supply Chain & Logistics  
Date: November 1, 2024  

**MARS PETCARE US, INC.**

By: ___________________________________  
Name: Marcus Vance  
Title: Vice President, Commercial Sales & Supply Chain  
Date: November 1, 2024  
"""
