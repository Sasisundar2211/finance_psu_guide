from django import forms

from .models import UserProfile


class ProfileForm(forms.ModelForm):
    """REQ-DASH-04: the phone number is the only editable profile field.

    Email is displayed from the stock User and password changes stay with
    allauth, so neither is a form field here.
    """

    class Meta:
        model = UserProfile
        fields = ["phone_number"]
        widgets = {
            "phone_number": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "tel"}
            )
        }
