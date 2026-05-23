(function() {
    console.log("🚀 Mouss Tec Supreme POS Engine FULLY OPERATED (Agents Linked, AI & Performance Activated)...");

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

    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }
    const csrftoken = getCookie('mt_csrf') || getCookie('csrftoken');

    // =====================================================================
    // 🛡️ 1. درع حماية البيانات المؤقت (Tab-Aware LocalStorage Shield)
    // =====================================================================
    const DRAFT_KEY = 'mousstec_pos_draft_' + window.location.pathname; // 🚀 عزل المسودات
    
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
    // 🤖 4. رادار البيع المتقاطع (Heuristic Cross-Selling)
    // =====================================================================
    function runAICrossSellRadar(selectElement) {
        const selectedText = selectElement.options[selectElement.selectedIndex]?.text.toLowerCase() || '';
        let suggestion = "";
        
        if (selectedText.includes('تيل') || selectedText.includes('brake')) {
            suggestion = "✨ AI: يُنصح ببيع حسّاس الفرامل وسيرفيس الطنابير.";
        } else if (selectedText.includes('زيت') || selectedText.includes('oil')) {
            suggestion = "✨ AI: يُنصح ببيع فلتر الزيت وطبة الكارتيرة.";
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
    // 🌐 5. جسر وكيل سوق Mouss Tec (LIVE B2B Marketplace Agent Integration)
    // =====================================================================
    async function fetchB2BMarketData(productName) {
        try {
            // 🚀 دمج حقيقي مع مسار سوق الجملة اللي عملناه
            const response = await fetch(`/api/v1/b2b/market/search/?q=${encodeURIComponent(productName)}`, {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            });
            
            if (!response.ok) throw new Error("Network response was not ok");
            const data = await response.json();
            return data.results || [];
        } catch (error) {
            console.error("B2B Market Fetch Error:", error);
            return null;
        }
    }

    function openB2BMarketModal(productName) {
        const modalId = 'mouss-b2b-modal';
        if(document.getElementById(modalId)) return;

        const modalHtml = `
            <div id="${modalId}" style="position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:99999; display:flex; justify-content:center; align-items:center; opacity:0; transition: opacity 0.3s ease; font-family:Cairo, sans-serif;">
                <div style="background:#fff; width:90%; max-width:650px; border-radius:12px; padding:25px; box-shadow:0 15px 30px rgba(0,0,0,0.5); transform: translateY(-20px); transition: transform 0.3s ease;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <h3 style="margin:0; color:#4f46e5;"><i class="fas fa-globe-africa"></i> رادار Mouss Tec للسوق المركزي</h3>
                        <span onclick="closeB2BModal()" style="cursor:pointer; color:#999; font-size:24px;">&times;</span>
                    </div>
                    <p style="color:#64748b; font-size:13px; margin-top:5px;">البحث السحابي عن: <b style="color:#1e293b;">${productName}</b></p>
                    
                    <div id="b2b-results" style="margin:20px 0; padding:30px; background:#f8f9fa; border-radius:8px; font-size:14px; text-align:center;">
                        <i class="fas fa-circle-notch fa-spin fa-2x" style="color:#4f46e5;"></i>
                        <div style="margin-top:10px; color:#64748b;">جاري الاتصال بوكلاء السوق المركزي...</div>
                    </div>
                </div>
            </div>
        `;
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        
        setTimeout(() => {
            const modal = document.getElementById(modalId);
            if(modal) { modal.style.opacity = '1'; modal.children[0].style.transform = 'translateY(0)'; }
        }, 10);

        // 🚀 الجلب الفعلي للبيانات
        fetchB2BMarketData(productName).then(results => {
            const resultsDiv = document.getElementById('b2b-results');
            if(!resultsDiv) return;

            if (results && results.length > 0) {
                let htmlList = `<div style="text-align:right; color:#10b981; font-weight:bold; margin-bottom:15px;"><i class="fas fa-check-circle"></i> تم العثور على عروض تنافسية!</div>`;
                results.forEach(item => {
                    htmlList += `
                        <div style="text-align:right; font-size:13px; border-bottom:1px solid #e2e8f0; padding-bottom:10px; margin-bottom:10px;">
                            <b style="color:#1e293b;">${item.tenant_name || 'شركة معتمدة'}</b> <i class="fas fa-certificate text-primary" title="موثق"></i>
                            <div style="color:#059669; font-weight:bold; float:left;">${item.wholesale_price} ج.م</div>
                            <div style="clear:both; color:#64748b; font-size:11px; margin-top:4px;">الكمية: ${item.available_qty} | الحالة: ${item.condition}</div>
                        </div>`;
                });
                resultsDiv.innerHTML = htmlList;
            } else {
                resultsDiv.innerHTML = `<div style="color:#ef4444; font-weight:bold;"><i class="fas fa-exclamation-triangle"></i> القطعة غير متوفرة في السوق المركزي حالياً.</div>`;
            }
        });

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

    function escListener(e) { if (e.key === 'Escape') closeB2BModal(); }

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
    // 🔫 6. محرك الباركود الآلي (Hardware Scanner Auto-Injector API Hook)
    // =====================================================================
    let barcodeBuffer = '';
    let barcodeTimer;

    async function processBarcodeScan(barcode) {
        try {
            // 🚀 ضرب مسار الـ API الحقيقي الذي صنعناه
            const res = await fetch(`/api/v1/barcode-lookup/?code=${encodeURIComponent(barcode)}`, {
                headers: { 'Accept': 'application/json' }
            });
            
            if (res.status === 404) {
                alert(`⚠️ القطعة ذات الباركود (${barcode}) غير مسجلة بالموسوعة!`);
                return;
            }
            if (!res.ok) throw new Error("Error fetching barcode");
            
            const product = await res.json();
            
            // 🚀 أتمتة حقن الصنف في الفاتورة (Django Admin JS Interop)
            const addBtn = document.querySelector('.dynamic-items .add-row a') || document.querySelector('#items-group .add-row a');
            if (addBtn) {
                addBtn.click(); // يحفز جانجو لإضافة سطر جديد
                
                // انتظار جانجو ليرسم السطر الجديد
                setTimeout(() => {
                    const allRows = document.querySelectorAll('.dynamic-items:not(.empty-form), #items-group tr.form-row:not(.empty-form)');
                    const latestRow = allRows[allRows.length - 1]; // السطر الأخير
                    
                    if(latestRow) {
                        const selectEl = latestRow.querySelector('select[name$="-product"]');
                        const priceInput = latestRow.querySelector('input[name$="-unit_price"]');
                        const qtyInput = latestRow.querySelector('input[name$="-quantity"]');
                        
                        if (selectEl) {
                            // اختيار الصنف لو كان موجود في الـ Select
                            for (let i=0; i<selectEl.options.length; i++) {
                                if (selectEl.options[i].value == product.id) {
                                    selectEl.selectedIndex = i;
                                    break;
                                }
                            }
                        }
                        if (priceInput) priceInput.value = product.price;
                        if (qtyInput) qtyInput.value = 1;
                        
                        // 🤖 عرض مؤشر المرونة السعرية للـ AI إن وجد
                        if (product.elasticity_indicator > 1.2 && selectEl) {
                            let badge = document.createElement('div');
                            badge.style.cssText = "color:#d97706; font-size:11px; margin-top:5px; font-weight:bold;";
                            badge.innerHTML = `<i class="fas fa-fire"></i> طلب عالي بالسوق (مرونة ${product.elasticity_indicator})!`;
                            selectEl.parentNode.appendChild(badge);
                        }

                        optimizedLiveTotals();
                        console.log(`✅ Auto-Injected: ${product.name}`);
                    }
                }, 150);
            }
        } catch(e) {
            console.error("Barcode Integration Error:", e);
        }
    }

    document.addEventListener('keypress', function(e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        
        if (e.key === 'Enter') {
            if (barcodeBuffer.length > 3) {
                processBarcodeScan(barcodeBuffer);
                barcodeBuffer = '';
            }
        } else {
            barcodeBuffer += e.key;
            clearTimeout(barcodeTimer);
            barcodeTimer = setTimeout(() => { barcodeBuffer = ''; }, 50);
        }
    });

    // =====================================================================
    // ⌨️ 7. اختصارات لوحة المفاتيح الاحترافية (Power-User Hotkeys)
    // =====================================================================
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            const saveBtn = document.querySelector('input[name="_continue"]') || document.querySelector('input[name="_save"]');
            if(saveBtn) saveBtn.click();
        }
        if (e.altKey && e.key.toLowerCase() === 'n') {
            e.preventDefault();
            const addRowBtn = document.querySelector('.dynamic-items .add-row a') || document.querySelector('#items-group .add-row a');
            if(addRowBtn) addRowBtn.click();
        }
    });

    // =====================================================================
    // 🎙️ 8. مساعد نقطة البيع الصوتي (Voice-Command POS Assistant)
    // =====================================================================
    function injectVoiceAssistant() {
        if (!('webkitSpeechRecognition' in window)) return;

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
            
            if (transcript.includes('حفظ') || transcript.includes('اعتمد')) {
                const saveBtn = document.querySelector('input[name="_save"]');
                if(saveBtn) saveBtn.click();
            } else if (transcript.includes('صنف') || transcript.includes('اضافه')) {
                const addRowBtn = document.querySelector('.dynamic-items .add-row a') || document.querySelector('#items-group .add-row a');
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
    // 🚗 9. تصفية المركبات حسب العميل (Vehicle-Customer Dynamic Filter)
    // =====================================================================
    function setupVehicleCustomerFilter() {
        // Django admin autocomplete uses Select2 — listen for customer changes
        const customerSelect = document.querySelector('#id_customer, select[name="customer"]');
        if (!customerSelect) return;

        function filterVehicles(customerId) {
            if (!customerId) return;
            fetch(`/api/v1/vehicles/by-customer/${customerId}/`, {
                headers: { 'Accept': 'application/json' }
            })
            .then(r => r.json())
            .then(data => {
                const vehicleSelect = document.querySelector('#id_vehicle, select[name="vehicle"]');
                if (!vehicleSelect) return;

                // حفظ القيمة الحالية
                const currentVal = vehicleSelect.value;
                // مسح الخيارات القديمة (ما عدا الخيار الفارغ)
                const emptyOption = vehicleSelect.querySelector('option[value=""]');
                vehicleSelect.innerHTML = '';
                if (emptyOption) vehicleSelect.appendChild(emptyOption);
                else {
                    const opt = document.createElement('option');
                    opt.value = '';
                    opt.textContent = '---------';
                    vehicleSelect.appendChild(opt);
                }

                // إضافة مركبات العميل
                (data.results || []).forEach(v => {
                    const opt = document.createElement('option');
                    opt.value = v.id;
                    opt.textContent = v.text;
                    if (String(v.id) === String(currentVal)) opt.selected = true;
                    vehicleSelect.appendChild(opt);
                });

                // تحديث Select2 إن كان موجود
                if (window.django && window.django.jQuery) {
                    window.django.jQuery(vehicleSelect).trigger('change');
                }
            })
            .catch(err => console.warn('Vehicle filter error:', err));
        }

        // Listen to native change
        customerSelect.addEventListener('change', () => filterVehicles(customerSelect.value));

        // Listen to Select2 change (Django autocomplete)
        if (window.django && window.django.jQuery) {
            window.django.jQuery(customerSelect).on('select2:select select2:clear change', function() {
                filterVehicles(this.value);
            });
        }

        // تشغيل أولي إذا كان العميل محدد مسبقاً
        if (customerSelect.value) filterVehicles(customerSelect.value);
    }

    // =====================================================================
    // 📡 10. المستشعرات والمراقب الذكي (Mutation Observer & Init)
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
            optimizedSaveDraft(); 
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
        
        formContainer.addEventListener('submit', () => localStorage.removeItem(DRAFT_KEY));
    }

    const addItemBtns = document.querySelectorAll('.add-row a');
    addItemBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            setTimeout(() => {
                injectMarketplaceButtons();
                optimizedLiveTotals();
            }, 150);
        });
    });

    window.addEventListener('load', () => {
        restoreFormDraft();
        applyStrictHiding();
        injectMarketplaceButtons();
        optimizedLiveTotals();
        injectVoiceAssistant();
        setupVehicleCustomerFilter();
    });
    
})();