from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Product, User
from .mpesa import format_kenyan_phone


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True, widget=forms.EmailInput(attrs={"placeholder": "you@example.com", "autocomplete": "email"}))
    phone_number = forms.CharField(max_length=15, widget=forms.TextInput(attrs={"placeholder": "0712345678", "autocomplete": "tel"}))
    role = forms.ChoiceField(choices=User.ROLE_CHOICES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"placeholder": "Choose a username", "autocomplete": "username"})
        self.fields["password1"].widget.attrs.update({"placeholder": "Min. 8 characters", "autocomplete": "new-password"})
        self.fields["password2"].widget.attrs.update({"placeholder": "Enter password again", "autocomplete": "new-password"})

    class Meta:
        model = User
        fields = ("username", "email", "phone_number", "role")

    def clean_phone_number(self):
        phone = format_kenyan_phone(self.cleaned_data["phone_number"])
        if User.objects.filter(phone_number=phone).exists():
            raise forms.ValidationError("This phone number is already registered.")
        return phone


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("category", "name", "description", "price", "stock", "image")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image"].widget.attrs.update({"accept": "image/*", "capture": "environment", "class": "image-file-input"})
        self.fields["image"].label = "Product or ticket photo"
        self.fields["image"].widget.attrs["aria-describedby"] = "image-upload-help"