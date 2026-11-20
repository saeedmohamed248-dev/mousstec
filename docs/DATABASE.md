# 🗺️ خريطة قاعدة البيانات

> ⚙️ الملف ده **متولّد أوتوماتيك** من الموديلات — ماتعدّلوش بإيدك.
> بعد أي تغيير في موديل: `make db-map` (أو `python manage.py db_map`) واعمل commit للنتيجة.
> اختبار `tenancy.tests.test_db_map` بيفشل لو الملف مش متحدّث.

## إزاي القاعدة متقسّمة

- **PostgreSQL واحدة** فيها schema اسمها `public` للمنصة نفسها (الشركات، الباقات، الاشتراكات، السوق…)
  و**schema لكل شركة** (django-tenants) فيها بيانات الشركة بس (مخزن، فواتير، خزائن، موظفين…).
- التطبيق اللي في `SHARED_APPS` جداوله في `public`؛ اللي في `TENANT_APPS` جداوله بتتكرر جوه كل شركة.
- `migrate` العادي مقفول — `make migrate` (`migrate_schemas --shared` ثم `--tenant` ثم `refresh_tenant_template`).
- علاقة (FK) من جدول شركة لجدول في `public` مسموحة (زي `auth.User`)، والعكس **ممنوع** —
  جدول المنصة مايقدرش يشاور على صف جوه schema شركة.

## نظرة سريعة

| التطبيق | مكان الجداول | عدد الموديلات |
|---|---|---|
| `ai_rooms` | public (مشتركة للمنصة) | 1 |
| `blog` | public (مشتركة للمنصة) | 1 |
| `bmw_ecu` | جوه schema كل شركة | 17 |
| `clients` | public (مشتركة للمنصة) | 79 |
| `diagnostics_catalog` | public (مشتركة للمنصة) | 5 |
| `hr` | جوه schema كل شركة | 13 |
| `inventory` | جوه schema كل شركة | 70 |
| `messenger_bot` | public (مشتركة للمنصة) | 2 |
| `omnichannel` | public (مشتركة للمنصة) | 3 |
| `print_market` | public (مشتركة للمنصة) | 9 |
| `printing` | جوه schema كل شركة | 33 |
| `repair_atlas` | جوه schema كل شركة | 5 |
| `robot` | جوه schema كل شركة | 24 |
| `smart_diagnostics` | جوه schema كل شركة | 7 |
| `social_ads` | public (مشتركة للمنصة) | 5 |

## الموديلات حسب التطبيق

### `ai_rooms` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `AIRoomConversation` | `ai_rooms_airoomconversation` | auth.User |

### `blog` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `Article` | `blog_article` | — |

### `bmw_ecu` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `BmwEcuSettlement` | `bmw_ecu_bmwecusettlement` | — |
| `CodingEntitlementHold` | `bmw_ecu_codingentitlementhold` | — |
| `DiagnosticFeeCharge` | `bmw_ecu_diagnosticfeecharge` | — |
| `EcuBackupRef` | `bmw_ecu_ecubackupref` | — |
| `EcuHardwareProfile` | `bmw_ecu_ecuhardwareprofile` | — |
| `EcuPinoutDiagram` | `bmw_ecu_ecupinoutdiagram` | — |
| `EcuSession` | `bmw_ecu_ecusession` | — |
| `EcuStateChange` | `bmw_ecu_ecustatechange` | — |
| `ExecutionAttempt` | `bmw_ecu_executionattempt` | — |
| `Feature` | `bmw_ecu_feature` | — |
| `FeatureUsageEvent` | `bmw_ecu_featureusageevent` | — |
| `GiftCredit` | `bmw_ecu_giftcredit` | — |
| `GiftCreditUsage` | `bmw_ecu_giftcreditusage` | — |
| `SubscriptionPackage` | `bmw_ecu_subscriptionpackage` | — |
| `TenantFeatureGrant` | `bmw_ecu_tenantfeaturegrant` | — |
| `TenantPackageGrant` | `bmw_ecu_tenantpackagegrant` | — |
| `WizardSession` | `bmw_ecu_wizardsession` | — |

### `clients` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `AIAddonPackage` | `clients_aiaddonpackage` | — |
| `AIBonusGrant` | `clients_aibonusgrant` | auth.User |
| `AILimitTracker` | `clients_ailimittracker` | — |
| `AIPromptLearningLog` | `clients_aipromptlearninglog` | auth.User |
| `AIStudioSession` | `clients_aistudiosession` | auth.User |
| `B2BPurchaseRequest` | `clients_b2bpurchaserequest` | — |
| `BidOffer` | `clients_bidoffer` | — |
| `BlindBiddingRequest` | `clients_blindbiddingrequest` | — |
| `BlockedIP` | `clients_blockedip` | — |
| `BroadcastCampaign` | `clients_broadcastcampaign` | auth.User |
| `BroadcastDismissal` | `clients_broadcastdismissal` | auth.User |
| `CatalogItem` | `clients_catalogitem` | — |
| `CatalogQuoteRequest` | `clients_catalogquoterequest` | — |
| `CatalogReview` | `clients_catalogreview` | — |
| `CatalogStore` | `clients_catalogstore` | — |
| `ChatMessage` | `clients_chatmessage` | — |
| `ChatSession` | `clients_chatsession` | auth.User |
| `Client` | `clients_client` | auth.User |
| `Coupon` | `clients_coupon` | auth.User |
| `CustomerBrandProfile` | `clients_customerbrandprofile` | — |
| `CustomerDesign` | `clients_customerdesign` | — |
| `CustomerDiagnosticsSubscription` | `clients_customerdiagnosticssubscription` | — |
| `CustomerNotification` | `clients_customernotification` | auth.User |
| `DesignChatMessage` | `clients_designchatmessage` | — |
| `DesignConversation` | `clients_designconversation` | — |
| `DesignConversationTurn` | `clients_designconversationturn` | — |
| `DesignPackage` | `clients_designpackage` | — |
| `DesignPrintRequest` | `clients_designprintrequest` | — |
| `DesignPromptLog` | `clients_designpromptlog` | — |
| `DesignPurchase` | `clients_designpurchase` | — |
| `DiagnosticsAddon` | `clients_diagnosticsaddon` | — |
| `DiagnosticsTopUpPack` | `clients_diagnosticstopuppack` | — |
| `DisputeEvidence` | `clients_disputeevidence` | — |
| `DisputeTicket` | `clients_disputeticket` | auth.User |
| `Domain` | `clients_domain` | — |
| `EscrowHold` | `clients_escrowhold` | auth.User |
| `EscrowLedger` | `clients_escrowledger` | — |
| `Feature` | `clients_feature` | — |
| `GlobalB2BMarketplace` | `clients_globalb2bmarketplace` | — |
| `ImportAssistRequest` | `clients_importassistrequest` | — |
| `ManualPaymentReceipt` | `clients_manualpaymentreceipt` | auth.User |
| `MarketplaceCustomer` | `clients_marketplacecustomer` | auth.User |
| `MarketplacePayout` | `clients_marketplacepayout` | auth.User |
| `OBDDevice` | `clients_obddevice` | — |
| `OBDDeviceNonce` | `clients_obddevicenonce` | — |
| `PartCarMake` | `clients_partcarmake` | — |
| `PartListing` | `clients_partlisting` | auth.User |
| `PartListingPhoto` | `clients_partlistingphoto` | — |
| `PartListingWatch` | `clients_partlistingwatch` | — |
| `PartOrder` | `clients_partorder` | auth.User |
| `PartWantedOffer` | `clients_partwantedoffer` | — |
| `PartWantedRequest` | `clients_partwantedrequest` | auth.User |
| `Plan` | `clients_plan` | — |
| `PlanPromotion` | `clients_planpromotion` | auth.User |
| `PlanRevision` | `clients_planrevision` | auth.User |
| `PlatformEvent` | `clients_platformevent` | — |
| `PlatformExpense` | `clients_platformexpense` | auth.User |
| `PlatformIncome` | `clients_platformincome` | auth.User |
| `PlatformInvoice` | `clients_platforminvoice` | auth.User |
| `PlatformLiabilityDisclaimer` | `clients_platformliabilitydisclaimer` | — |
| `PlatformModule` | `clients_platformmodule` | auth.User |
| `PlatformUserMFA` | `clients_platformusermfa` | auth.User |
| `ReferralReward` | `clients_referralreward` | — |
| `SavedPaymentCard` | `clients_savedpaymentcard` | — |
| `ScreenChunk` | `clients_screenchunk` | — |
| `ScreenSession` | `clients_screensession` | — |
| `ServiceRequest` | `clients_servicerequest` | — |
| `SignupAttribution` | `clients_signupattribution` | — |
| `StaffRole` | `clients_staffrole` | auth.User |
| `SupportAttachment` | `clients_supportattachment` | — |
| `SupportReply` | `clients_supportreply` | — |
| `SupportTicket` | `clients_supportticket` | auth.User |
| `SystemErrorLog` | `clients_systemerrorlog` | auth.User |
| `TenantDesignTopUp` | `clients_tenantdesigntopup` | — |
| `TenantSubscription` | `clients_tenantsubscription` | — |
| `TenderOffer` | `clients_tenderoffer` | — |
| `UserVerification` | `clients_userverification` | auth.User |
| `VisitorLog` | `clients_visitorlog` | auth.User |
| `WixConnection` | `clients_wixconnection` | — |

### `diagnostics_catalog` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `APICostRate` | `diagnostics_catalog_apicostrate` | — |
| `DTCDefinition` | `diagnostics_catalog_dtcdefinition` | — |
| `DTCExternalLookupCache` | `diagnostics_catalog_dtcexternallookupcache` | — |
| `VINDecodeCache` | `diagnostics_catalog_vindecodecache` | — |
| `VehicleProtocolMemory` | `diagnostics_catalog_vehicleprotocolmemory` | — |

### `hr` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `AIDesignSubscription` | `hr_aidesignsubscription` | auth.User |
| `Advance` | `hr_advance` | — |
| `AdvanceInstallment` | `hr_advanceinstallment` | — |
| `AttendanceRecord` | `hr_attendancerecord` | — |
| `DesignSubmission` | `hr_designsubmission` | — |
| `Employee` | `hr_employee` | auth.User |
| `EmployeeShiftAssignment` | `hr_employeeshiftassignment` | — |
| `HRSettings` | `hr_hrsettings` | — |
| `LeaveRequest` | `hr_leaverequest` | — |
| `PayrollEntry` | `hr_payrollentry` | — |
| `PayrollRun` | `hr_payrollrun` | auth.User |
| `PublicHoliday` | `hr_publicholiday` | — |
| `WorkShift` | `hr_workshift` | — |

### `inventory` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `AccountingEntry` | `inventory_accountingentry` | auth.User |
| `AccountingPeriod` | `inventory_accountingperiod` | auth.User |
| `AttendanceCheckIn` | `inventory_attendancecheckin` | — |
| `AuditLog` | `inventory_auditlog` | auth.User |
| `B2BListingRequest` | `inventory_b2blistingrequest` | auth.User |
| `BankStatement` | `inventory_bankstatement` | auth.User |
| `BankStatementLine` | `inventory_bankstatementline` | — |
| `Branch` | `inventory_branch` | — |
| `BranchAccess` | `inventory_branchaccess` | — |
| `ChartOfAccount` | `inventory_chartofaccount` | — |
| `Customer` | `inventory_customer` | — |
| `CustomerFeedback` | `inventory_customerfeedback` | — |
| `DisassemblyEvent` | `inventory_disassemblyevent` | auth.User |
| `DisassemblyResult` | `inventory_disassemblyresult` | — |
| `DisassemblyTemplate` | `inventory_disassemblytemplate` | — |
| `DocumentSequence` | `inventory_documentsequence` | — |
| `ETASettings` | `inventory_etasettings` | — |
| `EmployeeProfile` | `inventory_employeeprofile` | auth.User |
| `EmployeeShift` | `inventory_employeeshift` | — |
| `ExpenseCategory` | `inventory_expensecategory` | — |
| `FinancialTransaction` | `inventory_financialtransaction` | — |
| `FiscalYear` | `inventory_fiscalyear` | auth.User |
| `HistoricalFinancialTransaction` | `inventory_historicalfinancialtransaction` | auth.User |
| `HistoricalProduct` | `inventory_historicalproduct` | auth.User |
| `HistoricalStockTransfer` | `inventory_historicalstocktransfer` | auth.User |
| `HistoricalTreasury` | `inventory_historicaltreasury` | auth.User |
| `ImportSession` | `inventory_importsession` | auth.User |
| `Inventory` | `inventory_inventory` | — |
| `InventoryItem` | `inventory_inventoryitem` | — |
| `InventoryMovement` | `inventory_inventorymovement` | auth.User |
| `JournalEntry` | `inventory_journalentry` | auth.User |
| `MaintenanceContract` | `inventory_maintenancecontract` | — |
| `PartReturnGuard` | `inventory_partreturnguard` | — |
| `PartReturnPhoto` | `inventory_partreturnphoto` | auth.User |
| `Product` | `inventory_product` | — |
| `ProductImage` | `inventory_productimage` | — |
| `ProductPriceHistory` | `inventory_productpricehistory` | — |
| `PurchaseInvoice` | `inventory_purchaseinvoice` | — |
| `PurchaseInvoiceExtraCost` | `inventory_purchaseinvoiceextracost` | — |
| `PurchaseInvoiceItem` | `inventory_purchaseinvoiceitem` | — |
| `PurchaseOrder` | `inventory_purchaseorder` | auth.User |
| `PurchaseOrderItem` | `inventory_purchaseorderitem` | — |
| `PurchaseShipment` | `inventory_purchaseshipment` | auth.User |
| `PurchaseShipmentCost` | `inventory_purchaseshipmentcost` | — |
| `RFQ` | `inventory_rfq` | auth.User |
| `RFQQuote` | `inventory_rfqquote` | — |
| `RepairLog` | `inventory_repairlog` | — |
| `RepairLogMedia` | `inventory_repairlogmedia` | — |
| `SaleInvoice` | `inventory_saleinvoice` | — |
| `SaleInvoiceItem` | `inventory_saleinvoiceitem` | — |
| `SaleInvoicePhoto` | `inventory_saleinvoicephoto` | — |
| `SaleInvoiceServiceItem` | `inventory_saleinvoiceserviceitem` | — |
| `ScrapDismantlingJob` | `inventory_scrapdismantlingjob` | — |
| `ScrapDismantlingYield` | `inventory_scrapdismantlingyield` | — |
| `SerialNumber` | `inventory_serialnumber` | — |
| `ServiceCatalog` | `inventory_servicecatalog` | — |
| `ServiceNudge` | `inventory_servicenudge` | auth.User |
| `ServiceReminderRule` | `inventory_servicereminderrule` | — |
| `StockAlert` | `inventory_stockalert` | — |
| `StockTransfer` | `inventory_stocktransfer` | — |
| `TaxRate` | `inventory_taxrate` | — |
| `TemplateItem` | `inventory_templateitem` | — |
| `Treasury` | `inventory_treasury` | — |
| `UserMFA` | `inventory_usermfa` | auth.User |
| `Vehicle` | `inventory_vehicle` | — |
| `VehicleDiagnosticPhoto` | `inventory_vehiclediagnosticphoto` | — |
| `VehicleDiagnosticReport` | `inventory_vehiclediagnosticreport` | auth.User |
| `VehicleInspection` | `inventory_vehicleinspection` | — |
| `VehicleTelemetryLog` | `inventory_vehicletelemetrylog` | — |
| `Vendor` | `inventory_vendor` | — |

### `messenger_bot` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `ConversationLog` | `messenger_bot_conversationlog` | — |
| `SystemUpdate` | `messenger_bot_systemupdate` | — |

### `omnichannel` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `ChannelMessageLog` | `omnichannel_channelmessagelog` | clients.Client |
| `TenantChannelConfig` | `omnichannel_tenantchannelconfig` | clients.Client |
| `TenantChannelNumber` | `omnichannel_tenantchannelnumber` | — |

### `print_market` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `MarketAccount` | `print_market_marketaccount` | — |
| `MarketMessage` | `print_market_marketmessage` | — |
| `MarketNotification` | `print_market_marketnotification` | — |
| `MarketOffer` | `print_market_marketoffer` | — |
| `MarketOrder` | `print_market_marketorder` | — |
| `MarketPayment` | `print_market_marketpayment` | auth.User |
| `MarketProduct` | `print_market_marketproduct` | — |
| `MarketProof` | `print_market_marketproof` | — |
| `MarketReview` | `print_market_marketreview` | — |

### `printing` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `Designer` | `printing_designer` | auth.User |
| `DesignerWorkLog` | `printing_designerworklog` | — |
| `MachineProfile` | `printing_machineprofile` | — |
| `MaterialPurchase` | `printing_materialpurchase` | auth.User |
| `MaterialPurchaseLine` | `printing_materialpurchaseline` | — |
| `MaterialPurchaseReturn` | `printing_materialpurchasereturn` | auth.User |
| `MaterialPurchaseReturnLine` | `printing_materialpurchasereturnline` | — |
| `PriceQuotation` | `printing_pricequotation` | auth.User |
| `PrintBankReconciliation` | `printing_printbankreconciliation` | auth.User |
| `PrintBranch` | `printing_printbranch` | — |
| `PrintCheque` | `printing_printcheque` | auth.User |
| `PrintCreditAllocation` | `printing_printcreditallocation` | auth.User |
| `PrintCreditNote` | `printing_printcreditnote` | auth.User |
| `PrintCustomer` | `printing_printcustomer` | — |
| `PrintExpenseBill` | `printing_printexpensebill` | auth.User |
| `PrintFixedAsset` | `printing_printfixedasset` | auth.User |
| `PrintJob` | `printing_printjob` | — |
| `PrintJobMaterial` | `printing_printjobmaterial` | — |
| `PrintJournalBranch` | `printing_printjournalbranch` | inventory.JournalEntry |
| `PrintLedgerState` | `printing_printledgerstate` | — |
| `PrintMaterial` | `printing_printmaterial` | — |
| `PrintOrder` | `printing_printorder` | — |
| `PrintProof` | `printing_printproof` | auth.User |
| `PrintStockAdjustment` | `printing_printstockadjustment` | auth.User |
| `PrintSupplier` | `printing_printsupplier` | — |
| `PrintSupplierAllocation` | `printing_printsupplierallocation` | auth.User |
| `PrintTransaction` | `printing_printtransaction` | auth.User |
| `PrintTreasury` | `printing_printtreasury` | — |
| `PrintTreasuryTransfer` | `printing_printtreasurytransfer` | auth.User |
| `PrintVatReturn` | `printing_printvatreturn` | auth.User |
| `ProductType` | `printing_producttype` | — |
| `QuotationLine` | `printing_quotationline` | — |
| `StaffPermission` | `printing_staffpermission` | auth.User |

### `repair_atlas` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `RepairAnswer` | `repair_atlas_repairanswer` | auth.User |
| `RepairQuery` | `repair_atlas_repairquery` | — |
| `RepairSession` | `repair_atlas_repairsession` | auth.User |
| `TechPhoto` | `repair_atlas_techphoto` | — |
| `VerifiedKnowledge` | `repair_atlas_verifiedknowledge` | auth.User |

### `robot` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `MotorCommandLog` | `robot_motorcommandlog` | auth.User |
| `ProcurementSignal` | `robot_procurementsignal` | inventory.Branch, inventory.Product |
| `RobotAccessLog` | `robot_robotaccesslog` | hr.Employee |
| `RobotAlert` | `robot_robotalert` | — |
| `RobotCommand` | `robot_robotcommand` | auth.User |
| `RobotCustomerFace` | `robot_robotcustomerface` | inventory.Customer |
| `RobotDevice` | `robot_robotdevice` | inventory.Branch |
| `RobotEntry` | `robot_robotentry` | hr.Employee, inventory.Branch, inventory.FinancialTransaction, inventory.PurchaseInvoice, inventory.SaleInvoice |
| `RobotFaceEnrollment` | `robot_robotfaceenrollment` | auth.User |
| `RobotHeard` | `robot_robotheard` | — |
| `RobotHeardFix` | `robot_robotheardfix` | — |
| `RobotKnowledge` | `robot_robotknowledge` | hr.Employee, inventory.Product |
| `RobotPageCall` | `robot_robotpagecall` | auth.User, hr.Employee |
| `RobotReply` | `robot_robotreply` | auth.User, inventory.Branch |
| `RobotRestockRequest` | `robot_robotrestockrequest` | inventory.Branch, inventory.Customer, inventory.Product |
| `RobotScanEvent` | `robot_robotscanevent` | hr.Employee, inventory.Product, inventory.SaleInvoice |
| `RobotSnapshot` | `robot_robotsnapshot` | auth.User |
| `RobotStaffPin` | `robot_robotstaffpin` | auth.User, hr.Employee |
| `RobotStockTakeLine` | `robot_robotstocktakeline` | inventory.Product |
| `RobotStockTakeSession` | `robot_robotstocktakesession` | hr.Employee, inventory.Branch |
| `RobotSyncEvent` | `robot_robotsyncevent` | — |
| `RobotTeacherUsage` | `robot_robotteacherusage` | — |
| `RobotUnderstanding` | `robot_robotunderstanding` | — |
| `RobotVoiceInteraction` | `robot_robotvoiceinteraction` | hr.Employee |

### `smart_diagnostics` — جوه schema كل شركة

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `APICallLog` | `smart_diagnostics_apicalllog` | auth.User |
| `DiagnosticDevice` | `smart_diagnostics_diagnosticdevice` | inventory.Vehicle |
| `DiagnosticScan` | `smart_diagnostics_diagnosticscan` | auth.User, inventory.Vehicle |
| `FaultLog` | `smart_diagnostics_faultlog` | auth.User, inventory.Vehicle |
| `LiveTelemetryFrame` | `smart_diagnostics_livetelemetryframe` | — |
| `TestPlanExecution` | `smart_diagnostics_testplanexecution` | — |
| `TestStepResult` | `smart_diagnostics_teststepresult` | — |

### `social_ads` — public (مشتركة للمنصة)

| الموديل | الجدول | علاقات بتطبيقات تانية |
|---|---|---|
| `AdCampaign` | `social_ads_adcampaign` | clients.Client |
| `PerformanceSnapshot` | `social_ads_performancesnapshot` | clients.Client |
| `SocialAdsConfig` | `social_ads_socialadsconfig` | clients.Client |
| `SocialPost` | `social_ads_socialpost` | clients.Client |
| `StrategyMemory` | `social_ads_strategymemory` | clients.Client |
