from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0045_product_part_category_and_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="branchaccess",
            name="price_view",
            field=models.CharField(
                choices=[
                    ("both", "التكلفة والبيع"),
                    ("sale", "سعر البيع فقط"),
                    ("cost", "سعر التكلفة فقط"),
                ],
                default="sale",
                help_text="يتحكّم في الأسعار اللي الموظف يشوفها لمخزون الفرع ده.",
                max_length=5,
                verbose_name="الأسعار الظاهرة في هذا الفرع",
            ),
        ),
        migrations.AlterField(
            model_name="employeeprofile",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "مالك المنشأة (Owner)"),
                    ("admin", "مدير عام (أدمن)"),
                    ("manager", "مدير فرع (Branch Manager)"),
                    ("supervisor", "مشرف (Supervisor)"),
                    ("accountant", "محاسب (Accountant)"),
                    ("sales", "مبيعات (Sales)"),
                    ("purchasing", "مشتريات (Procurement)"),
                    ("engineer", "مهندس تشخيص (Engineer)"),
                    ("tech", "فني / ميكانيكي (Technician)"),
                    ("cashier", "كاشير / استقبال (Cashier)"),
                    ("stock", "أمين مخزن (Warehouse)"),
                    ("hr", "موارد بشرية (HR)"),
                    ("viewer", "مراجع / عرض فقط (Auditor)"),
                ],
                default="cashier",
                max_length=20,
                verbose_name="الدور الوظيفي",
            ),
        ),
    ]
