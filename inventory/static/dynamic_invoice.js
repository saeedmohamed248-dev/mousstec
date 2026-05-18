(function() {
    console.log("🚀 Mouss Tec Supreme POS Engine FULLY OPERATIONAL (AI, Hotkeys & Performance Activated)...");

    // =====================================================================
    // 🧠 0. خوارزميات الأداء والمساعدة (Performance Utilities)
    // =====================================================================
    function debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    }

    // =====================================================================
    // 🛡️ 1. درع حماية البيانات المؤقت (Auto-Draft LocalStorage Shield)
    // =====================================================================
    const DRAFT_KEY = 'mousstec_pos_draft';
    
    function saveFormDraft() {
        const draftData = {};
        const formElements = document.querySelectorAll('input:not([type="hidden"]), select, textarea');
        formElements.forEach(el => {
            if (el.name && !el.name.includes('csrf')) {
                draftData[el.name] = el.type === 'checkbox' ? el.checked : el.value;
            }
        });
        localStorage.setItem(DRAFT_KEY, JSON.stringify(draftData));
    }

    function restoreFormDraft() {
        const savedDraft = localStorage.getItem(DRAFT_KEY);
        if (savedDraft) {
            const draftData = JSON.parse(savedDraft);
            if (Object.keys(draftData).length > 2 && confirm("🔄 تم اكتشاف فاتورة غير مكتملة. هل تريد استرجاع البيانات المفقودة؟")) {
                Object.keys(draftData).forEach(key => {
                    const el = document.querySelector(`[name="${key}"]`);
                    if (el) {
                        if (el.type === 'checkbox') el.checked = draftData[key];
                        else el.value = draftData[key];
                    }
                });
                optimizedLiveTotals();
                applyStrictHiding();
            }
        }
    }

    const optimizedSaveDraft = debounce(saveFormDraft, 1000);

    // =====================================================================
    // 🛠️ 2. محرك الإخفاء الصارم للحقول والتبويبات (Smart UI Guard)
    // =====================================================================
    function applyStrictHiding() {
        const typeSelect = document.querySelector('#id_invoice_type') || document.querySelector('[id$="invoice_type"]');
        if (!typeSelect) return;

        const isSaleOnly = (typeSelect.value === 'sale');

        const targetFields = [
            document.querySelector('.field-vehicle'),
            document.querySelector('.field-mileage'),
            document.querySelector('.field-next_visit_date'),
            document.querySelector('#id_vehicle')
        ];

        targetFields.forEach(el => {
            if (el) {
                const row = el.closest('.form-row, .form-group, .col-12') || el;
                if (isSaleOnly) {
                    row.style.setProperty('display', 'none', 'important');
                } else {
                    row.style.setProperty('display', '', 'important');
                }
            }
        });

        const tabLinks = document.querySelectorAll('.nav-tabs .nav-link, .nav-tabs .nav-item, ul.nav-tabs a, [role="tab"]');
        tabLinks.forEach(tab => {
            const text = tab.textContent || '';
            if (text.includes('الخدمات') || text.includes('المصنعيات') || text.includes('الفحص') || text.includes('DVI')) {
                const parentLi = tab.closest('li, .nav-item') || tab;
                if (isSaleOnly) {
                    parentLi.style.setProperty('display', 'none', 'important');
                    if (parentLi.classList.contains('active') || tab.classList.contains('active')) {
                        const firstTab = document.querySelector('.nav-tabs .nav-link, ul.nav-tabs a');
                        if (firstTab) firstTab.click();
                    }
                } else {
                    parentLi.style.setProperty('display', '', 'important');
                }
            }
        });

        const inlineContainers = document.querySelectorAll('#saleinvoiceserviceitem_set-group, #vehicleinspection-group, [id*="serviceitem"], [id*="inspection"]');
        inlineContainers.forEach(box => {
            if (isSaleOnly) box.style.setProperty('display', 'none', 'important');
            else box.style.setProperty('display', '', 'important');
        });
    }

    // =====================================================================
    // 🧮 3. الآلة الحاسبة الحية ورادار الأرباح (Live Real-time Calculator)
    // =====================================================================
    function calculateLiveTotals() {
        let grandTotal = 0;
        const itemRows = document.querySelectorAll('.dynamic-items, tr.form-row, .formset-row'); 
        
        itemRows.forEach(row => {
            if (row.classList.contains('empty-form') || row.classList.contains('deleted')) return;

            const qtyInput = row.querySelector('input[name$="-quantity"]');
            const priceInput = row.querySelector('input[name$="-unit_price"]');
            const totalDisplay = row.querySelector('.field-get_total_price b, .column-get_total_price, td.field-get_total_price, .field-total_price');

            if (qtyInput && priceInput) {
                const qty = parseFloat(qtyInput.value) || 0;
                const price = parseFloat(priceInput.value) || 0;
                const rowTotal = qty * price;

                // 🚨 رادار حماية المبيعات
                if (price <= 0 && qty > 0) {
                    priceInput.style.border = "2px solid #dc3545";
                    priceInput.style.backgroundColor = "#fff5f5";
                } else {
                    priceInput.style.border = "";
                    priceInput.style.backgroundColor = "";
                }

                if (totalDisplay) {
                    const bTag = totalDisplay.querySelector('b') || totalDisplay;
                    bTag.innerText = rowTotal.toLocaleString('en-US', { minimumFractionDigits: 2 }) + " ج.م";
                }
                grandTotal += rowTotal;
            }
        });

        const typeSelect = document.querySelector('#id_invoice_type') || document.querySelector('[id$="invoice_type"]');
        if (typeSelect && typeSelect.value !== 'sale') {
            const serviceRows = document.querySelectorAll('#saleinvoiceserviceitem_set-group tr.form-row, .dynamic-service_items');
            serviceRows.forEach(row => {
                if (row.classList.contains('empty-form') || row.classList.contains('deleted')) return;
                const priceInput = row.querySelector('input[name$="-price"]');
                if (priceInput) grandTotal += parseFloat(priceInput.value) || 0;
            });
        }

        const manualLabor = parseFloat(document.querySelector('#id_labor_cost_manual')?.value) || 0;
        const discountInput = document.querySelector('#id_discount');
        const discount = parseFloat(discountInput?.value) || 0;
        const taxPercentage = parseFloat(document.querySelector('#id_tax_percentage')?.value) || 0;

        let subTotalForCheck = grandTotal + (typeSelect && typeSelect.value !== 'sale' ? manualLabor : 0);
        
        // 📉 درع حماية هوامش الربح
        if (discountInput) {
            if (subTotalForCheck > 0 && discount > (subTotalForCheck * 0.20)) {
                discountInput.style.border = "2px solid #ffc107";
                discountInput.style.backgroundColor = "#fffbeb";
                discountInput.title = "⚠️ تحذير: الخصم تجاوز 20% من قيمة الفاتورة!";
            } else {
                discountInput.style.border = "";
                discountInput.style.backgroundColor = "";
                discountInput.title = "";
            }
        }

        let subTotal = subTotalForCheck - discount;
        let taxAmount = (subTotal * taxPercentage) / 100;
        let finalTotal = subTotal + taxAmount;

        const finalTotalField = document.querySelector('.field-total_amount div, .readonly, #id_total_amount');
        if (finalTotalField && !finalTotalField.querySelector('input')) {
            finalTotalField.innerHTML = `<b style="color:#28a745; font-size:18px;">${finalTotal.toLocaleString('en-US', { minimumFractionDigits: 2 })} ج.م</b> 
                                         <span style="font-size:11px; color:gray;">(تحديث حي)</span>`;
        }
    }
    const optimizedLiveTotals = debounce(calculateLiveTotals, 300);

    // =====================================================================
    // 🤖 4. رادار البيع المتقاطع والذكاء الاصطناعي (AI Cross-Selling)
    // =====================================================================
    function runAICrossSellRadar(selectElement) {
        const selectedText = selectElement.options[selectElement.selectedIndex]?.text.toLowerCase() || '';
        let suggestion = "";
        
        if (selectedText.includes('تيل') || selectedText.includes('brake')) {
            suggestion = "✨ AI: هل نسيت إضافة حسّاس الفرامل أو سيرفيس الطنابير؟";
        } else if (selectedText.includes('زيت') || selectedText.includes('oil')) {
            suggestion = "✨ AI: يُنصح ببيع فلتر الزيت وطبة الكارتيرة مع هذه القطعة.";
        }

        let badge = selectElement.parentNode.querySelector('.ai-cross-sell-badge');
        if (suggestion) {
            if (!badge) {
                badge = document.createElement('div');
                badge.className = "ai-cross-sell-badge";
                badge.style.cssText = "color:#6f42c1; font-size:10px; margin-top:4px; font-weight:bold; animation: pulse 2s infinite;";
                selectElement.parentNode.appendChild(badge);
            }
            badge.innerText = suggestion;
        } else if (badge) {
            badge.remove();
        }
    }

    // =====================================================================
    // 🌐 5. جسر سوق Mouss Tec (Live B2B Marketplace Injector)
    // =====================================================================
    function openB2BMarketModal(productName) {
        const modalId = 'mouss-b2b-modal';
        if(document.getElementById(modalId)) return;

        const modalHtml = `
            <div id="${modalId}" style="position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:99999; display:flex; justify-content:center; align-items:center; opacity:0; transition: opacity 0.3s ease;">
                <div style="background:#fff; width:90%; max-width:650px; border-radius:12px; padding:25px; box-shadow:0 15px 30px rgba(0,0,0,0.5); transform: translateY(-20px); transition: transform 0.3s ease;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <h3 style="margin:0; color:#4f46e5; font-family:Cairo;"><i class="fas fa-globe-africa"></i> رادار Mouss Tec للسوق المركزي</h3>
                        <span onclick="closeB2BModal()" style="cursor:pointer; color:#999; font-size:20px;">&times;</span>
                    </div>
                    <p style="color:#64748b; font-size:13px;">البحث السحابي عن: <b style="color:#1e293b;">${productName}</b></p>
                    
                    <div id="b2b-results" style="margin:20px 0; padding:30px; background:#f8f9fa; border-radius:8px; font-size:14px; text-align:center;">
                        <i class="fas fa-circle-notch fa-spin fa-2x" style="color:#4f46e5;"></i>
                        <div style="margin-top:10px; color:#64748b;">جاري مسح مخازن التجار الموثوقين...</div>
                    </div>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        
        // Trigger Animation
        setTimeout(() => {
            const modal = document.getElementById(modalId);
            modal.style.opacity = '1';
            modal.children[0].style.transform = 'translateY(0)';
        }, 10);

        // محاكاة الاتصال بالـ API الحقيقي
        setTimeout(() => {
            const resultsDiv = document.getElementById('b2b-results');
            if(resultsDiv) {
                resultsDiv.innerHTML = `
                    <div style="text-align:right; color:#10b981; font-weight:bold; margin-bottom:15px;"><i class="fas fa-check-circle"></i> تم العثور على بدائل تنافسية!</div>
                    <div style="text-align:right; font-size:13px; border-bottom:1px solid #e2e8f0; padding-bottom:8px; margin-bottom:8px;">
                        <b style="color:#1e293b;">الشركة الألمانية للاستيراد</b> <i class="fas fa-certificate text-primary" title="موثق"></i>
                        <div style="color:#059669; font-weight:bold; float:left;">1,200 ج.م</div>
                        <div style="clear:both;"></div>
                    </div>
                    <div style="text-align:right; font-size:13px; padding-bottom:8px;">
                        <b style="color:#1e293b;">مركز التوحيد (شحن آمن)</b>
                        <div style="color:#059669; font-weight:bold; float:left;">1,350 ج.م</div>
                        <div style="clear:both;"></div>
                    </div>
                `;
            }
        }, 1200);

        // إغلاق النافذة بزر Esc (Power User Feature)
        document.addEventListener('keydown', escListener);
    }

    window.closeB2BModal = function() {
        const modal = document.getElementById('mouss-b2b-modal');
        if(modal) {
            modal.style.opacity = '0';
            setTimeout(() => modal.remove(), 300);
        }
        document.removeEventListener('keydown', escListener);
    };

    function escListener(e) {
        if (e.key === 'Escape') closeB2BModal();
    }

    function injectMarketplaceButtons() {
        const productSelects = document.querySelectorAll('select[name$="-product"]');
        productSelects.forEach(select => {
            select.addEventListener('change', () => runAICrossSellRadar(select));

            if (!select.nextElementSibling || !select.nextElementSibling.classList.contains('mouss-tec-btn')) {
                const searchBtn = document.createElement('a');
                searchBtn.href = "javascript:void(0)";
                searchBtn.className = "mouss-tec-btn";
                searchBtn.innerHTML = '<i class="fas fa-search"></i> سوق B2B';
                searchBtn.style.cssText = "display:inline-block; margin-right:10px; font-size:11px; background:#4f46e5; color:white; padding:4px 8px; border-radius:4px; text-decoration:none; transition:0.3s; font-family:Cairo;";
                
                searchBtn.onclick = function(e) {
                    e.preventDefault();
                    const pName = select.options[select.selectedIndex]?.text || 'قطعة غير محددة';
                    openB2BMarketModal(pName);
                };
                select.parentNode.insertBefore(searchBtn, select.nextSibling);
            }
        });
    }

    // =====================================================================
    // 🔫 6. محرك الباركود السريع (Hardware Barcode Scanner Support)
    // =====================================================================
    let barcodeBuffer = '';
    let barcodeTimer;
    document.addEventListener('keypress', function(e) {
        // إذا كان المستخدم يكتب يدوياً في حقل نصي، لا تتدخل
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        
        if (e.key === 'Enter') {
            if (barcodeBuffer.length > 3) {
                console.log(`[Barcode Scanned]: ${barcodeBuffer}`);
                // هنا يتم المناداة على API الباركود لإضافة الصنف آلياً للفاتورة
                // fetch(`/api/v1/barcode-lookup/?code=${barcodeBuffer}`)
                barcodeBuffer = '';
            }
        } else {
            barcodeBuffer += e.key;
            clearTimeout(barcodeTimer);
            barcodeTimer = setTimeout(() => { barcodeBuffer = ''; }, 50); // مسدس الباركود يكتب بسرعة فائقة جداً
        }
    });

    // =====================================================================
    // ⌨️ 7. اختصارات لوحة المفاتيح الاحترافية (Power-User Hotkeys)
    // =====================================================================
    document.addEventListener('keydown', function(e) {
        // Ctrl+Enter للحفظ السريع (Save & Continue)
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            const saveBtn = document.querySelector('input[name="_continue"]') || document.querySelector('input[name="_save"]');
            if(saveBtn) saveBtn.click();
        }
        // Alt+N لإضافة سطر منتج جديد
        if (e.altKey && e.key.toLowerCase() === 'n') {
            e.preventDefault();
            const addRowBtn = document.querySelector('.add-row a');
            if(addRowBtn) addRowBtn.click();
        }
    });

    // =====================================================================
    // 🎙️ 8. مساعد نقطة البيع الصوتي (Voice-Command POS Assistant)
    // =====================================================================
    function injectVoiceAssistant() {
        if (!('webkitSpeechRecognition' in window)) return;

        const headerDiv = document.querySelector('#content > h1') || document.body;
        const voiceBtn = document.createElement('button');
        voiceBtn.innerHTML = '<i class="fas fa-microphone"></i>';
        voiceBtn.style.cssText = "position:fixed; bottom:20px; left:20px; width:50px; height:50px; border-radius:50%; background:#ec4899; color:white; border:none; box-shadow:0 4px 15px rgba(236,72,153,0.5); cursor:pointer; z-index:999; font-size:18px; transition:0.3s;";
        voiceBtn.title = "المساعد الصوتي للورشة";
        
        document.body.appendChild(voiceBtn);

        const recognition = new webkitSpeechRecognition();
        recognition.lang = 'ar-EG';
        recognition.continuous = false;

        recognition.onstart = function() {
            voiceBtn.style.transform = "scale(1.2)";
            voiceBtn.style.boxShadow = "0 0 20px rgba(236,72,153,0.8)";
            voiceBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        };

        recognition.onresult = function(event) {
            const transcript = event.results[0][0].transcript.toLowerCase();
            console.log("[Voice Command]:", transcript);
            // ابتكار: تنفيذ الأوامر بالذكاء الاصطناعي (مثل: "اضغط حفظ" أو "صنف جديد")
            if (transcript.includes('حفظ') || transcript.includes('اعتمد')) {
                const saveBtn = document.querySelector('input[name="_save"]');
                if(saveBtn) saveBtn.click();
            } else if (transcript.includes('صنف') || transcript.includes('اضافه')) {
                const addRowBtn = document.querySelector('.add-row a');
                if(addRowBtn) addRowBtn.click();
            }
        };

        recognition.onend = function() {
            voiceBtn.style.transform = "scale(1)";
            voiceBtn.style.boxShadow = "0 4px 15px rgba(236,72,153,0.5)";
            voiceBtn.innerHTML = '<i class="fas fa-microphone"></i>';
        };

        voiceBtn.onclick = (e) => { e.preventDefault(); recognition.start(); };
    }

    // =====================================================================
    // 📡 9. المستشعرات والمراقب الذكي (Mutation Observer & Initialization)
    // =====================================================================
    const uiObserver = new MutationObserver(function(mutations) {
        uiObserver.disconnect();
        applyStrictHiding();
        injectMarketplaceButtons();
        uiObserver.observe(document.documentElement, {
            childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style']
        });
    });

    uiObserver.observe(document.documentElement, {
        childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style']
    });

    const formContainer = document.querySelector('#saleinvoice_form, #change-form, form');
    if (formContainer) {
        formContainer.addEventListener('input', function(e) {
            optimizedSaveDraft(); // حفظ مسودة بعد كل نقرة
            if (e.target.name && (
                e.target.name.includes('quantity') || 
                e.target.name.includes('price') || 
                e.target.name.includes('discount') ||
                e.target.name.includes('labor') ||
                e.target.name.includes('tax')
            )) {
                optimizedLiveTotals();
            }
        });
        
        const typeSelect = document.querySelector('#id_invoice_type') || document.querySelector('[id$="invoice_type"]');
        if (typeSelect) {
            typeSelect.addEventListener('change', () => {
                applyStrictHiding();
                optimizedLiveTotals();
            });
        }
        
        // مسح المسودة عند الحفظ النهائي بنجاح
        formContainer.addEventListener('submit', () => localStorage.removeItem(DRAFT_KEY));
    }

    const addItemBtn = document.querySelector('.add-row a');
    if (addItemBtn) {
        addItemBtn.addEventListener('click', () => {
            setTimeout(() => {
                injectMarketplaceButtons();
                optimizedLiveTotals();
            }, 150);
        });
    }

    window.addEventListener('load', () => {
        restoreFormDraft();
        applyStrictHiding();
        injectMarketplaceButtons();
        optimizedLiveTotals();
        injectVoiceAssistant();
    });
    
})();