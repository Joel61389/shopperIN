
import base64
from io import BytesIO
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import requests
import qrcode
from qrcode.image.svg import SvgPathImage
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Case, Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Sum, Value, When
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .forms import ProductForm, RegisterForm
from .models import CoinTransaction, MpesaTransaction, Order, Product, Review, User
from .mpesa import format_kenyan_phone, initiate_stk_push

# create your views here.


def home(request):
    products = Product.objects.filter(is_active=True).annotate(
        average_rating_value=Avg("reviews__rating"),
        review_count_value=Count("reviews", distinct=True),
        is_featured=Case(
            When(featured_until__gt=timezone.now(), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    ).select_related("vendor").prefetch_related(Prefetch("reviews", queryset=Review.objects.select_related("user"))).order_by("-created_at")
    query = request.GET.get("q", "").strip()
    item_type = request.GET.get("type", "").lower()
    category = request.GET.get("category", "").upper()
    category_labels = {
        code: label
        for _, options in Product.CATEGORY_CHOICES
        for code, label in options
    }
    if query:
        products = products.filter(Q(name__icontains=query) | Q(description__icontains=query) | Q(vendor__username__icontains=query))
    if item_type == "products":
        products = products.exclude(category__in=Product.TICKET_CATEGORIES)
    elif item_type == "tickets":
        products = products.filter(category__in=Product.TICKET_CATEGORIES)
    else:
        item_type = ""
    if category in category_labels:
        products = products.filter(category=category)
    else:
        category = ""
    products = products.order_by("-is_featured", "-created_at")
    return render(request, "home.html", {
        "products": products,
        "query": query,
        "selected_type": item_type,
        "selected_category": category,
        "category_groups": Product.CATEGORY_CHOICES,
        "category_labels": category_labels,
        "rating_range": range(1, 6),
    })


def _cart_summary(cart, user=None):
    items = list(cart.values())
    total = sum((Decimal(item["price"]) * item["qty"] for item in items), Decimal("0.00"))
    payable_total = total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    coin_balance = user.coin_balance if user and user.is_authenticated else 0
    max_coin_discount = min(coin_balance, int((total * Decimal("0.20")).to_integral_value(rounding=ROUND_FLOOR)))
    return {
        "items": items,
        "count": sum(item["qty"] for item in items),
        "total": f"{payable_total:.2f}",
        "subtotal": f"{total:.2f}",
        "coin_balance": coin_balance,
        "max_coin_discount": max_coin_discount,
    }

def _cart_json(request):
    return JsonResponse(_cart_summary(request.session.get("cart", {}), request.user))


def _refund_order_coins(orders, reason):
    coins_to_refund = sum(order.coins_redeemed for order in orders)
    if coins_to_refund <= 0 or not orders:
        return
    buyer = User.objects.select_for_update().get(pk=orders[0].customer_id)
    buyer.coin_balance += coins_to_refund
    buyer.save(update_fields=["coin_balance"])
    CoinTransaction.objects.create(user=buyer, amount=coins_to_refund, reason=reason)


def cart_add(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    try:
        data = json.loads(request.body)
        product_id = int(data.get("product_id"))
        quantity = int(data.get("qty", 1))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid product or quantity."}, status=400)
    if quantity < 1:
        return JsonResponse({"error": "Quantity must be at least one."}, status=400)
    product = get_object_or_404(Product, id=product_id, is_active=True)
    cart = request.session.get("cart", {})
    key = str(product.id)
    current_quantity = cart.get(key, {}).get("qty", 0)
    if product.stock < current_quantity + quantity:
        return JsonResponse({"error": "Not enough stock available."}, status=400)
    cart[key] = {
        "product_id": product.id,
        "name": product.name,
        "price": str(product.price),
        "qty": current_quantity + quantity,
        "image": product.image.url if product.image else "",
        "is_ticket": product.is_ticket,
    }
    request.session["cart"] = cart
    return _cart_json(request)


def cart_update(request):
    if request.method == "GET":
        return _cart_json(request)
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    try:
        data = json.loads(request.body)
        product_id = int(data.get("product_id"))
        quantity = int(data.get("qty"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid product or quantity."}, status=400)
    cart = request.session.get("cart", {})
    key = str(product_id)
    if key not in cart:
        return JsonResponse({"error": "Item is not in your cart."}, status=404)
    if quantity <= 0:
        cart.pop(key)
    else:
        product = get_object_or_404(Product, id=product_id, is_active=True)
        if quantity > product.stock:
            return JsonResponse({"error": "Not enough stock available."}, status=400)
        cart[key]["qty"] = quantity
    request.session["cart"] = cart
    return _cart_json(request)


def cart_clear(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    request.session["cart"] = {}
    return _cart_json(request)


@login_required
def cart_checkout(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    cart = request.session.get("cart", {})
    if not cart:
        messages.error(request, "Your cart is empty.")
        return redirect("home")
    try:
        phone = format_kenyan_phone(request.POST.get("phone_number", request.user.phone_number))
    except (TypeError, AttributeError):
        messages.error(request, "Enter a valid Kenyan phone number.")
        return redirect("home")
    if not re.fullmatch(r"254[17]\d{8}", phone):
        messages.error(request, "Use a valid Safaricom phone number in 2547XXXXXXXX format.")
        return redirect("home")

    batch_reference = uuid.uuid4()
    coins_redeemed = 0
    with transaction.atomic():
        buyer = User.objects.select_for_update().get(pk=request.user.pk)
        products = {
            str(product.id): product
            for product in Product.objects.select_for_update().filter(
                id__in=[int(key) for key in cart], is_active=True
            )
        }
        if len(products) != len(cart):
            messages.error(request, "One or more cart listings are no longer available.")
            return redirect("home")
        for key, item in cart.items():
            if item["qty"] < 1 or products[key].stock < item["qty"]:
                messages.error(request, f"Not enough stock for {products[key].name}.")
                return redirect("home")

        exact_total = sum((products[key].price * item["qty"] for key, item in cart.items()), Decimal("0.00"))
        if request.POST.get("redeem_coins") == "1":
            max_discount = int((exact_total * Decimal("0.20")).to_integral_value(rounding=ROUND_FLOOR))
            coins_redeemed = min(buyer.coin_balance, max_discount)
            if coins_redeemed:
                buyer.coin_balance -= coins_redeemed
                buyer.save(update_fields=["coin_balance"])
                CoinTransaction.objects.create(
                    user=buyer,
                    amount=-coins_redeemed,
                    reason="Redeemed on M-Pesa cart checkout",
                )
        charge_total = (exact_total - coins_redeemed).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        orders = []
        for index, (key, item) in enumerate(cart.items()):
            line_total = products[key].price * item["qty"]
            orders.append(Order.objects.create(
                customer=request.user,
                product=products[key],
                quantity=item["qty"],
                total_amount=line_total,
                phone_number=phone,
                batch_reference=batch_reference,
                coins_redeemed=coins_redeemed if index == 0 else 0,
            ))

    try:
        stk_response = initiate_stk_push(
            phone_number=phone,
            amount=int(charge_total),
            account_ref=f"SHOPIN{str(batch_reference).replace('-', '')[:6]}",
            description="ShopperIN Cart",
        )
    except Exception:
        with transaction.atomic():
            failed_orders = list(Order.objects.select_for_update().filter(batch_reference=batch_reference))
            Order.objects.filter(batch_reference=batch_reference).update(status="FAILED")
            _refund_order_coins(failed_orders, "Refund for failed M-Pesa checkout")
        messages.error(request, "Could not reach M-Pesa. Please try again.")
        return redirect("home")

    if stk_response.get("ResponseCode") == "0" and stk_response.get("CheckoutRequestID"):
        MpesaTransaction.objects.create(
            order=orders[0],
            batch_reference=batch_reference,
            merchant_request_id=stk_response.get("MerchantRequestID", ""),
            checkout_request_id=stk_response["CheckoutRequestID"],
        )
        request.session["cart"] = {}
        return redirect("payment_waiting", order_id=orders[0].id)

    with transaction.atomic():
        failed_orders = list(Order.objects.select_for_update().filter(batch_reference=batch_reference))
        Order.objects.filter(batch_reference=batch_reference).update(status="FAILED")
        _refund_order_coins(failed_orders, "Refund for failed M-Pesa checkout")
    messages.error(request, stk_response.get("errorMessage", "STK Push rejected by Safaricom."))
    return redirect("home")


@login_required
def submit_review(request, product_id):
    if request.method != "POST":
        return redirect("order_history")
    product = get_object_or_404(Product, id=product_id)
    if not Order.objects.filter(customer=request.user, product=product, status="PAID").exists():
        return HttpResponseForbidden("Only verified buyers can review this listing.")
    try:
        rating = int(request.POST.get("rating", ""))
    except (TypeError, ValueError):
        messages.error(request, "Choose a rating from 1 to 5 stars.")
        return redirect("order_history")
    if rating not in range(1, 6):
        messages.error(request, "Choose a rating from 1 to 5 stars.")
        return redirect("order_history")
    comment = request.POST.get("comment", "").strip()[:500]
    with transaction.atomic():
        review, created = Review.objects.update_or_create(
            product=product,
            user=request.user,
            defaults={"rating": rating, "comment": comment},
        )
        if created:
            buyer = User.objects.select_for_update().get(pk=request.user.pk)
            buyer.coin_balance += 15
            buyer.save(update_fields=["coin_balance"])
            CoinTransaction.objects.create(user=buyer, amount=15, reason="Verified purchase review")
            if rating == 5:
                vendor = User.objects.select_for_update().get(pk=product.vendor_id)
                vendor.coin_balance += 10
                vendor.save(update_fields=["coin_balance"])
                CoinTransaction.objects.create(user=vendor, amount=10, reason="Received a 5-star review")
    messages.success(request, f"Your review for {product.name} has been saved.")
    return redirect("order_history")


def register_view(request):
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("vendor_dashboard" if user.role == "VENDOR" else "home")
    else:
        form = RegisterForm()
    return render(request, "register.html", {"form": form})


@login_required
def vendor_dashboard(request):
    if request.user.role != "VENDOR":
        return HttpResponseForbidden("Only Vendor accounts can access this page.")

    if request.method == "POST" and request.POST.get("action") == "update_stock":
        product = get_object_or_404(Product, id=request.POST.get("product_id"), vendor=request.user)
        try:
            stock = int(request.POST.get("stock", ""))
            if stock < 0:
                raise ValueError
        except ValueError:
            messages.error(request, "Enter a valid stock count of zero or more.")
        else:
            product.stock = stock
            product.save(update_fields=["stock"])
            messages.success(request, f"Stock updated for {product.name}.")
        return redirect("vendor_dashboard")

    if request.method == "POST" and request.POST.get("action") == "request_verification":
        identity_document = request.FILES.get("id_document")
        if not identity_document:
            messages.error(request, "Choose an ID document to submit for review.")
        else:
            request.user.id_document = identity_document
            request.user.save(update_fields=["id_document"])
            messages.success(request, "Your ID document was submitted for verification review.")
        return redirect("vendor_dashboard")

    if request.method == "POST" and request.POST.get("action") == "delete_product":
        product = get_object_or_404(Product, id=request.POST.get("product_id"), vendor=request.user)
        if Order.objects.filter(product=product).exists():
            messages.error(request, "This listing has order history and cannot be deleted. You can mark it inactive instead.")
            product.is_active = False
            product.save(update_fields=["is_active"])
        else:
            product.delete()
            messages.success(request, "Listing deleted.")
        return redirect("vendor_dashboard")

    if request.method == "POST" and request.POST.get("action") == "toggle_listing":
        product = get_object_or_404(Product, id=request.POST.get("product_id"), vendor=request.user)
        product.is_active = not product.is_active
        product.save(update_fields=["is_active"])
        messages.success(request, f"{product.name} is now {'active' if product.is_active else 'inactive'}.")
        return redirect("vendor_dashboard")

    editing_product = None
    if request.method == "POST":
        product_id = request.POST.get("product_id")
        if product_id:
            editing_product = get_object_or_404(Product, id=product_id, vendor=request.user)
        form = ProductForm(request.POST, request.FILES, instance=editing_product)
        if form.is_valid():
            product = form.save(commit=False)
            if not editing_product:
                product.vendor = request.user
            product.save()
            draft_identifier = str(editing_product.pk) if editing_product else "new"
            request.session["vendor_saved_draft"] = f"dukapay-vendor-draft-{request.user.pk}-{draft_identifier}"
            messages.success(request, "Listing updated." if editing_product else "Product added successfully!")
            return redirect("vendor_dashboard")
    else:
        edit_id = request.GET.get("edit")
        if edit_id:
            editing_product = get_object_or_404(Product, id=edit_id, vendor=request.user)
        form = ProductForm(instance=editing_product)

    my_products = Product.objects.filter(vendor=request.user).order_by("-created_at")
    total_revenue = Order.objects.filter(product__vendor=request.user, status="PAID").aggregate(total=Sum("total_amount"))["total"] or 0
    verification_reviews = Review.objects.filter(product__vendor=request.user).aggregate(
        average=Avg("rating"), count=Count("id")
    )
    paid_orders_count = Order.objects.filter(product__vendor=request.user, status="PAID").count()
    active_products_count = my_products.filter(is_active=True).count()
    clear_vendor_draft = request.session.pop("vendor_saved_draft", "")
    return render(request, "vendor_dashboard.html", {
        "form": form,
        "editing_product": editing_product,
        "products": my_products,
        "total_listings": my_products.count(),
        "total_stock": my_products.aggregate(total=Sum("stock"))["total"] or 0,
        "total_revenue": total_revenue,
        "paid_orders_count": paid_orders_count,
        "verification_average_rating": verification_reviews["average"] or 0,
        "verification_review_count": verification_reviews["count"],
        "active_products_count": active_products_count,
        "verification_eligible": request.user.check_verification_eligibility(),
        "clear_vendor_draft": clear_vendor_draft,
        "current_time": timezone.now(),
    })


@login_required
def feature_product(request, product_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if request.user.role != "VENDOR":
        return HttpResponseForbidden("Only Vendor accounts can feature listings.")
    with transaction.atomic():
        vendor = User.objects.select_for_update().get(pk=request.user.pk)
        product = get_object_or_404(Product.objects.select_for_update(), id=product_id, vendor=vendor)
        if vendor.coin_balance < 100:
            messages.error(request, "You need 100 DukaCoins to feature a listing.")
        else:
            vendor.coin_balance -= 100
            vendor.save(update_fields=["coin_balance"])
            product.featured_until = timezone.now() + timedelta(hours=48)
            product.save(update_fields=["featured_until"])
            CoinTransaction.objects.create(user=vendor, amount=-100, reason=f"Featured listing: {product.name}")
            messages.success(request, f"{product.name} is featured on the storefront for 48 hours.")
    return redirect("vendor_dashboard")


@login_required
def generate_description(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)
    if request.user.role != "VENDOR":
        return JsonResponse({"error": "Vendor access required."}, status=403)

    image_file = None
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "Send valid JSON."}, status=400)
    elif request.content_type == "multipart/form-data":
        data = request.POST
        image_file = request.FILES.get("image")
    else:
        return JsonResponse({"error": "Send JSON or multipart form data."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Send valid listing details."}, status=400)

    name = str(data.get("name", "")).strip()[:200]
    price = str(data.get("price", "")).strip()[:30]
    category = str(data.get("category", "")).strip()[:40]
    if not name or not category:
        return JsonResponse({"error": "Enter a name and listing type first."}, status=400)
    if image_file and not (image_file.content_type or "").startswith("image/"):
        return JsonResponse({"error": "Choose a valid image file."}, status=400)

    category_labels = {
        code: label
        for _, options in Product.CATEGORY_CHOICES
        for code, label in options
    }
    category_display = category_labels.get(category, category)
    price_copy = f" at KES {price}" if price else ""
    fallback = (
        f"Let the music rise and the night glow with {name}. Explore this {category_display.lower()} listing in Kenya{price_copy}."
        if category in Product.TICKET_CATEGORIES
        else f"Like a bright note in the everyday, {name} brings a little market-day spark. Find this {category_display.lower()} listing in Kenya{price_copy}."
    )
    if not settings.GEMINI_API_KEY:
        return JsonResponse({"description": fallback, "fallback": True})

    prompt = (
        "Visually inspect the attached image first when one is provided. Compare what is actually visible "
        "against the typed product name and selected category. If the image clearly does not match the "
        "typed product name or category, do not invent specifications; return only a JSON object with "
        "mismatch set to true and message set to exactly: \"Heads up: the uploaded photo looks like "
        "[what is actually in the image], which doesn't match '[name]'. Please double-check your photo "
        "or product title.\" "
        "If the image matches, or no image is attached, return JSON with mismatch set to false and a description. "
        "The description must be 2 or 3 sentences for a Kenyan marketplace or event listing. Begin with 1 or 2 "
        "rhythmic, vivid, poetic lines that spark desire, then give grounded, truthful details visible in the "
        "photo (such as color, packaging, condition, or flyer details) without fabricating unseen claims. "
        "Without an image, use only the typed facts. Keep the copy persuasive but honest. "
        f"Name: {name}. Category: {category_display}. Price: {price or 'not supplied'}. "
        'Return exactly one JSON object: {"mismatch": false, "description": "..."} or '
        '{"mismatch": true, "message": "..."}.'
    )
    parts = [{"text": prompt}]
    if image_file:
        parts.append({
            "inline_data": {
                "mime_type": image_file.content_type,
                "data": base64.b64encode(image_file.read()).decode("ascii"),
            }
        })
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers={"x-goog-api-key": settings.GEMINI_API_KEY},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=12,
        )
        response.raise_for_status()
        result = response.json()
        response_parts = result["candidates"][0]["content"]["parts"]
        response_text = " ".join(
            part["text"].strip()
            for part in response_parts
            if isinstance(part, dict)
            and not part.get("thought")
            and isinstance(part.get("text"), str)
            and part["text"].strip()
        )
        try:
            generated = json.loads(response_text)
        except json.JSONDecodeError:
            generated = {"description": response_text}
        if generated.get("mismatch") is True:
            message = str(generated.get("message", "The photo may not match this listing. Please double-check it."))
            return JsonResponse({"mismatch": True, "message": message})
        description = str(generated.get("description", "")).strip()
        if not description:
            raise ValueError("Gemini returned an empty description.")
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return JsonResponse({"description": fallback, "fallback": True})

    return JsonResponse({"description": description, "fallback": False})


@login_required
def checkout_stk_push(request, product_id):
    if request.method != "POST":
        return redirect("home")
    product = get_object_or_404(Product, id=product_id, is_active=True)
    try:
        quantity = int(request.POST.get("quantity", 1))
        phone = format_kenyan_phone(request.POST.get("phone_number", request.user.phone_number))
    except (TypeError, ValueError):
        messages.error(request, "Enter a valid quantity and Kenyan phone number.")
        return redirect("home")

    if quantity < 1 or not re.fullmatch(r"254[17]\d{8}", phone):
        messages.error(request, "Use a valid Safaricom phone number in 2547XXXXXXXX format and a quantity of at least one.")
        return redirect("home")

    total_amount = (product.price * quantity).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if product.stock < quantity:
        messages.error(request, "Not enough stock available.")
        return redirect("home")

    order = Order.objects.create(
        customer=request.user,
        product=product,
        quantity=quantity,
        total_amount=total_amount,
        phone_number=phone,
    )

    try:
        stk_response = initiate_stk_push(
            phone_number=phone,
            amount=int(total_amount),
            account_ref=f"SHOPIN{str(order.id).replace('-', '')[:6]}",
            description="ShopperIN Order",
        )
    except Exception:
        order.status = "FAILED"
        order.save(update_fields=["status"])
        messages.error(request, "Could not reach M-Pesa. Please try again.")
        return redirect("home")

    if stk_response.get("ResponseCode") == "0" and stk_response.get("CheckoutRequestID"):
        MpesaTransaction.objects.create(
            order=order,
            merchant_request_id=stk_response.get("MerchantRequestID", ""),
            checkout_request_id=stk_response["CheckoutRequestID"],
        )
        return redirect("payment_waiting", order_id=order.id)

    order.status = "FAILED"
    order.save(update_fields=["status"])
    messages.error(request, stk_response.get("errorMessage", "STK Push rejected by Safaricom."))
    return redirect("home")


@login_required
def payment_waiting(request, order_id):
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    batch_orders = Order.objects.filter(batch_reference=order.batch_reference) if order.batch_reference else Order.objects.filter(id=order.id)
    batch_total = batch_orders.aggregate(total=Sum("total_amount"))["total"] or 0
    coins_redeemed = batch_orders.aggregate(total=Sum("coins_redeemed"))["total"] or 0
    payment_amount = (batch_total - coins_redeemed).quantize(Decimal("1"), rounding=ROUND_HALF_UP) if isinstance(batch_total, Decimal) else batch_total - coins_redeemed
    return render(request, "waiting.html", {"order": order, "batch_orders": batch_orders, "batch_total": batch_total, "coins_redeemed": coins_redeemed, "payment_amount": payment_amount})


@login_required
def check_order_status(request, order_id):
    order = get_object_or_404(Order, id=order_id, customer=request.user)
    receipt = getattr(getattr(order, "mpesa_tx", None), "mpesa_receipt_number", None)
    return JsonResponse({"status": order.status, "receipt": receipt})


@login_required
def order_history(request):
    receipt_query = MpesaTransaction.objects.filter(
        Q(order_id=OuterRef("pk")) | (Q(batch_reference=OuterRef("batch_reference")) & Q(batch_reference__isnull=False))
    ).values("mpesa_receipt_number")[:1]
    orders = Order.objects.filter(customer=request.user).annotate(
        mpesa_receipt=Subquery(receipt_query)
    ).select_related("product", "product__vendor").prefetch_related(
        Prefetch("product__reviews", queryset=Review.objects.filter(user=request.user), to_attr="my_reviews")
    ).order_by("-created_at")
    for order in orders:
        if order.status == "PAID" and order.product.is_ticket:
            pass_data = f"ShopperIN event pass\nOrder: {order.id}\nM-Pesa receipt: {order.mpesa_receipt or 'pending'}"
            qr = qrcode.make(pass_data, image_factory=SvgPathImage, box_size=5, border=2)
            output = BytesIO()
            qr.save(output)
            order.pass_qr_data = base64.b64encode(output.getvalue()).decode("ascii")
    return render(request, "orders.html", {
        "orders": orders,
        "coin_transactions": request.user.coin_transactions.all()[:10],
    })


@login_required
def receipt(request, order_id):
    order = get_object_or_404(Order.objects.select_related("product"), id=order_id, customer=request.user)
    batch_orders = Order.objects.filter(batch_reference=order.batch_reference).select_related("product") if order.batch_reference else Order.objects.filter(id=order.id).select_related("product")
    batch_total = batch_orders.aggregate(total=Sum("total_amount"))["total"] or 0
    coins_redeemed = batch_orders.aggregate(total=Sum("coins_redeemed"))["total"] or 0
    payment_amount = (batch_total - coins_redeemed).quantize(Decimal("1"), rounding=ROUND_HALF_UP) if isinstance(batch_total, Decimal) else batch_total - coins_redeemed
    tx_query = MpesaTransaction.objects.filter(batch_reference=order.batch_reference) if order.batch_reference else MpesaTransaction.objects.filter(order=order)
    tx = tx_query.first()
    return render(request, "receipt.html", {
        "order": order,
        "batch_orders": batch_orders,
        "batch_total": batch_total,
        "coins_redeemed": coins_redeemed,
        "payment_amount": payment_amount,
        "receipt_code": tx.mpesa_receipt_number if tx else None,
    })


@csrf_exempt
def mpesa_callback(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    data = json.loads(request.body.decode("utf-8"))
    stk_callback = data.get("Body", {}).get("stkCallback", {})
    checkout_id = stk_callback.get("CheckoutRequestID")
    result_code = stk_callback.get("ResultCode")
    result_desc = stk_callback.get("ResultDesc", "")

    with transaction.atomic():
        tx = MpesaTransaction.objects.select_for_update().filter(checkout_request_id=checkout_id).first()
        if tx and tx.order.status == "PENDING":
            tx.result_code = result_code
            tx.result_desc = result_desc
            tx.raw_callback = data

            if result_code == 0:
                items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
                meta = {item["Name"]: item.get("Value") for item in items}
                tx.mpesa_receipt_number = meta.get("MpesaReceiptNumber")

                order_query = Order.objects.filter(batch_reference=tx.batch_reference) if tx.batch_reference else Order.objects.filter(id=tx.order_id)
                orders = list(order_query.select_related("product"))
                for order in orders:
                    order.status = "PAID"
                    order.save(update_fields=["status"])
                    product = Product.objects.select_for_update().get(id=order.product_id)
                    product.stock = max(0, product.stock - order.quantity)
                    product.save(update_fields=["stock"])
                paid_total = sum((order.total_amount for order in orders), Decimal("0.00")) - sum(
                    order.coins_redeemed for order in orders
                )
                coins_earned = int(paid_total // Decimal("100"))
                if coins_earned:
                    buyer = User.objects.select_for_update().get(pk=orders[0].customer_id)
                    buyer.coin_balance += coins_earned
                    buyer.save(update_fields=["coin_balance"])
                    CoinTransaction.objects.create(
                        user=buyer,
                        amount=coins_earned,
                        reason="1 DukaCoin per KES 100 paid",
                    )
            else:
                if tx.batch_reference:
                    failed_orders = list(
                        Order.objects.select_for_update().filter(batch_reference=tx.batch_reference, status="PENDING")
                    )
                    Order.objects.filter(batch_reference=tx.batch_reference, status="PENDING").update(status="FAILED")
                else:
                    failed_orders = [tx.order] if tx.order.status == "PENDING" else []
                    tx.order.status = "FAILED"
                    tx.order.save(update_fields=["status"])
                _refund_order_coins(failed_orders, "Refund for failed M-Pesa payment")

            tx.save()

    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})