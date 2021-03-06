# Copyright (c) The Johns Hopkins University/Applied Physics Laboratory
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Support signature verification."""

import binascii
import datetime

from castellan import key_manager
from cryptography import exceptions as crypto_exception
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import dsa
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography import x509
import debtcollector
from oslo_log import log as logging
from oslo_serialization import base64
from oslo_utils import encodeutils

from glance.common import exception
from glance.i18n import _, _LE

LOG = logging.getLogger(__name__)


# Note: This is the signature hash method, which is independent from the
# image data checksum hash method (which is handled elsewhere).
HASH_METHODS = {
    'SHA-224': hashes.SHA224(),
    'SHA-256': hashes.SHA256(),
    'SHA-384': hashes.SHA384(),
    'SHA-512': hashes.SHA512()
}

# Currently supported signature key types
# RSA Options
RSA_PSS = 'RSA-PSS'

# DSA Options
DSA = 'DSA'

# ECC curves -- note that only those with key sizes >=384 are included
# Note also that some of these may not be supported by the cryptography backend
ECC_CURVES = (
    ec.SECT571K1(),
    ec.SECT409K1(),
    ec.SECT571R1(),
    ec.SECT409R1(),
    ec.SECP521R1(),
    ec.SECP384R1(),
)

# These are the currently supported certificate formats
(X_509,) = (
    'X.509',
)

CERTIFICATE_FORMATS = {
    X_509
}

# These are the currently supported MGF formats, used for RSA-PSS signatures
MASK_GEN_ALGORITHMS = {
    'MGF1': padding.MGF1
}

# Required image property names
(SIGNATURE, HASH_METHOD, KEY_TYPE, CERT_UUID) = (
    'img_signature',
    'img_signature_hash_method',
    'img_signature_key_type',
    'img_signature_certificate_uuid'
)

# TODO(bpoulos): remove when 'sign-the-hash' approach is no longer supported
(OLD_SIGNATURE, OLD_HASH_METHOD, OLD_KEY_TYPE, OLD_CERT_UUID) = (
    'signature',
    'signature_hash_method',
    'signature_key_type',
    'signature_certificate_uuid'
)

# Optional image property names for RSA-PSS
# TODO(bpoulos): remove when 'sign-the-hash' approach is no longer supported
(MASK_GEN_ALG, PSS_SALT_LENGTH) = (
    'mask_gen_algorithm',
    'pss_salt_length'
)


class SignatureKeyType(object):

    _REGISTERED_TYPES = {}

    def __init__(self, name, public_key_type, create_verifier):
        self.name = name
        self.public_key_type = public_key_type
        self.create_verifier = create_verifier

    @classmethod
    def register(cls, name, public_key_type, create_verifier):
        """Register a signature key type.

        :param name: the name of the signature key type
        :param public_key_type: e.g. RSAPublicKey, DSAPublicKey, etc.
        :param create_verifier: a function to create a verifier for this type
        """
        cls._REGISTERED_TYPES[name] = cls(name,
                                          public_key_type,
                                          create_verifier)

    @classmethod
    def lookup(cls, name):
        """Look up the signature key type.

        :param name: the name of the signature key type
        :returns: the SignatureKeyType object
        :raises: glance.common.exception.SignatureVerificationError if
                 signature key type is invalid
        """
        if name not in cls._REGISTERED_TYPES:
            raise exception.SignatureVerificationError(
                _('Invalid signature key type: %s') % name
            )
        return cls._REGISTERED_TYPES[name]


# each key type will require its own verifier
def create_verifier_for_pss(signature, hash_method, public_key,
                            image_properties):
    """Create the verifier to use when the key type is RSA-PSS.

    :param signature: the decoded signature to use
    :param hash_method: the hash method to use, as a cryptography object
    :param public_key: the public key to use, as a cryptography object
    :param image_properties: the key-value properties about the image
    :returns: the verifier to use to verify the signature for RSA-PSS
    :raises glance.common.exception.SignatureVerificationError: if the
            RSA-PSS specific properties are invalid
    """
    # retrieve other needed properties, or use defaults if not there
    if MASK_GEN_ALG in image_properties:
        mask_gen_algorithm = image_properties[MASK_GEN_ALG]
        if mask_gen_algorithm not in MASK_GEN_ALGORITHMS:
            raise exception.SignatureVerificationError(
                _('Invalid mask_gen_algorithm: %s') % mask_gen_algorithm
            )
        mgf = MASK_GEN_ALGORITHMS[mask_gen_algorithm](hash_method)
    else:
        # default to MGF1
        mgf = padding.MGF1(hash_method)

    if PSS_SALT_LENGTH in image_properties:
        pss_salt_length = image_properties[PSS_SALT_LENGTH]
        try:
            salt_length = int(pss_salt_length)
        except ValueError:
            raise exception.SignatureVerificationError(
                _('Invalid pss_salt_length: %s') % pss_salt_length
            )
    else:
        # default to max salt length
        salt_length = padding.PSS.MAX_LENGTH

    # return the verifier
    return public_key.verifier(
        signature,
        padding.PSS(mgf=mgf, salt_length=salt_length),
        hash_method
    )


def create_verifier_for_ecc(signature, hash_method, public_key,
                            image_properties):
    """Create the verifier to use when the key type is ECC_*.

    :param signature: the decoded signature to use
    :param hash_method: the hash method to use, as a cryptography object
    :param public_key: the public key to use, as a cryptography object
    :param image_properties: the key-value properties about the image
    :return: the verifier to use to verify the signature for ECC_*
    """
    # return the verifier
    return public_key.verifier(
        signature,
        ec.ECDSA(hash_method)
    )


def create_verifier_for_dsa(signature, hash_method, public_key,
                            image_properties):
    """Create verifier to use when the key type is DSA

    :param signature: the decoded signature to use
    :param hash_method: the hash method to use, as a cryptography object
    :param public_key: the public key to use, as a cryptography object
    :param image_properties: the key-value properties about the image
    :returns: the verifier to use to verify the signature for DSA
    """
    # return the verifier
    return public_key.verifier(
        signature,
        hash_method
    )


# map the key type to the verifier function to use
SignatureKeyType.register(RSA_PSS, rsa.RSAPublicKey, create_verifier_for_pss)
SignatureKeyType.register(DSA, dsa.DSAPublicKey, create_verifier_for_dsa)

# Register the elliptic curves which are supported by the backend
for curve in ECC_CURVES:
    if default_backend().elliptic_curve_supported(curve):
        SignatureKeyType.register('ECC_' + curve.name.upper(),
                                  ec.EllipticCurvePublicKey,
                                  create_verifier_for_ecc)


def should_create_verifier(image_properties):
    """Determine whether a verifier should be created.

    Using the image properties, determine whether existing properties indicate
    that signature verification should be done.

    :param image_properties: the key-value properties about the image
    :return: True, if signature metadata properties exist, False otherwise
    """
    return (image_properties is not None and
            CERT_UUID in image_properties and
            HASH_METHOD in image_properties and
            SIGNATURE in image_properties and
            KEY_TYPE in image_properties)


def get_verifier(context, image_properties):
    """Retrieve the image properties and use them to create a verifier.

    :param context: the user context for authentication
    :param image_properties: the key-value properties about the image
    :return: instance of cryptography AsymmetricVerificationContext
    :raises glance.common.exception.SignatureVerificationError: if building
            the verifier fails
    """
    if not should_create_verifier(image_properties):
        raise exception.SignatureVerificationError(
            _('Required image properties for signature verification do not'
              ' exist. Cannot verify signature.')
        )

    signature = get_signature(image_properties[SIGNATURE])
    hash_method = get_hash_method(image_properties[HASH_METHOD])
    signature_key_type = SignatureKeyType.lookup(
        image_properties[KEY_TYPE])
    public_key = get_public_key(context,
                                image_properties[CERT_UUID],
                                signature_key_type)

    # create the verifier based on the signature key type
    try:
        verifier = signature_key_type.create_verifier(signature,
                                                      hash_method,
                                                      public_key,
                                                      image_properties)
    except crypto_exception.UnsupportedAlgorithm as e:
        msg = (_LE("Unable to create verifier since algorithm is "
                   "unsupported: %(e)s")
               % {'e': encodeutils.exception_to_unicode(e)})
        LOG.error(msg)
        raise exception.SignatureVerificationError(
            _('Unable to verify signature since the algorithm is unsupported '
              'on this system')
        )

    if verifier:
        return verifier
    else:
        # Error creating the verifier
        raise exception.SignatureVerificationError(
            _('Error occurred while creating the verifier')
        )


@debtcollector.removals.remove(message="This will be removed in the N cycle.")
def should_verify_signature(image_properties):
    """Determine whether a signature should be verified.

    Using the image properties, determine whether existing properties indicate
    that signature verification should be done.

    :param image_properties: the key-value properties about the image
    :returns: True, if signature metadata properties exist, False otherwise
    """
    return (image_properties is not None and
            OLD_CERT_UUID in image_properties and
            OLD_HASH_METHOD in image_properties and
            OLD_SIGNATURE in image_properties and
            OLD_KEY_TYPE in image_properties)


@debtcollector.removals.remove(
    message="Starting with the Mitaka release, this approach to signature "
            "verification using the image 'checksum' and signature metadata "
            "properties that do not start with 'img' will not be supported. "
            "This functionality will be removed in the N release. This "
            "approach is being replaced with a signature of the data "
            "directly, instead of a signature of the hash method, and the new "
            "approach uses properties that start with 'img_'.")
def verify_signature(context, checksum_hash, image_properties):
    """Retrieve the image properties and use them to verify the signature.

    :param context: the user context for authentication
    :param checksum_hash: the 'checksum' hash of the image data
    :param image_properties: the key-value properties about the image
    :returns: True if verification succeeds
    :raises glance.common.exception.SignatureVerificationError:
            if verification fails
    """
    if not should_verify_signature(image_properties):
        raise exception.SignatureVerificationError(
            _('Required image properties for signature verification do not'
              ' exist. Cannot verify signature.')
        )

    checksum_hash = encodeutils.to_utf8(checksum_hash)

    signature = get_signature(image_properties[OLD_SIGNATURE])
    hash_method = get_hash_method(image_properties[OLD_HASH_METHOD])
    signature_key_type = SignatureKeyType.lookup(
        image_properties[OLD_KEY_TYPE])
    public_key = get_public_key(context,
                                image_properties[OLD_CERT_UUID],
                                signature_key_type)

    # create the verifier based on the signature key type
    try:
        verifier = signature_key_type.create_verifier(signature,
                                                      hash_method,
                                                      public_key,
                                                      image_properties)
    except crypto_exception.UnsupportedAlgorithm as e:
        msg = (_LE("Unable to create verifier since algorithm is "
                   "unsupported: %(e)s")
               % {'e': encodeutils.exception_to_unicode(e)})
        LOG.error(msg)
        raise exception.SignatureVerificationError(
            _('Unable to verify signature since the algorithm is unsupported '
              'on this system')
        )

    if verifier:
        # Verify the signature
        verifier.update(checksum_hash)
        try:
            verifier.verify()
            return True
        except crypto_exception.InvalidSignature:
            raise exception.SignatureVerificationError(
                _('Signature verification failed.')
            )
    else:
        # Error creating the verifier
        raise exception.SignatureVerificationError(
            _('Error occurred while verifying the signature')
        )


def get_signature(signature_data):
    """Decode the signature data and returns the signature.

    :param siganture_data: the base64-encoded signature data
    :returns: the decoded signature
    :raises glance.common.exception.SignatureVerificationError: if the
            signature data is malformatted
    """
    try:
        signature = base64.decode_as_bytes(signature_data)
    except (TypeError, binascii.Error):
        raise exception.SignatureVerificationError(
            _('The signature data was not properly encoded using base64')
        )

    return signature


def get_hash_method(hash_method_name):
    """Verify the hash method name and create the hash method.

    :param hash_method_name: the name of the hash method to retrieve
    :returns: the hash method, a cryptography object
    :raises glance.common.exception.SignatureVerificationError: if the
            hash method name is invalid
    """
    if hash_method_name not in HASH_METHODS:
        raise exception.SignatureVerificationError(
            _('Invalid signature hash method: %s') % hash_method_name
        )

    return HASH_METHODS[hash_method_name]


def get_public_key(context, signature_certificate_uuid, signature_key_type):
    """Create the public key object from a retrieved certificate.

    :param context: the user context for authentication
    :param signature_certificate_uuid: the uuid to use to retrieve the
                                       certificate
    :param signature_key_type: a SignatureKeyType object
    :returns: the public key cryptography object
    :raises glance.common.exception.SignatureVerificationError: if public
            key format is invalid
    """
    certificate = get_certificate(context, signature_certificate_uuid)

    # Note that this public key could either be
    # RSAPublicKey, DSAPublicKey, or EllipticCurvePublicKey
    public_key = certificate.public_key()

    # Confirm the type is of the type expected based on the signature key type
    if not isinstance(public_key, signature_key_type.public_key_type):
        raise exception.SignatureVerificationError(
            _('Invalid public key type for signature key type: %s')
            % signature_key_type
        )

    return public_key


def get_certificate(context, signature_certificate_uuid):
    """Create the certificate object from the retrieved certificate data.

    :param context: the user context for authentication
    :param signature_certificate_uuid: the uuid to use to retrieve the
                                       certificate
    :returns: the certificate cryptography object
    :raises glance.common.exception.SignatureVerificationError: if the
            retrieval fails or the format is invalid
    """
    keymgr_api = key_manager.API()

    try:
        # The certificate retrieved here is a castellan certificate object
        cert = keymgr_api.get(context, signature_certificate_uuid)
    except Exception as e:
        # The problem encountered may be backend-specific, since castellan
        # can use different backends.  Rather than importing all possible
        # backends here, the generic "Exception" is used.
        msg = (_LE("Unable to retrieve certificate with ID %(id)s: %(e)s")
               % {'id': signature_certificate_uuid,
                  'e': encodeutils.exception_to_unicode(e)})
        LOG.error(msg)
        raise exception.SignatureVerificationError(
            _('Unable to retrieve certificate with ID: %s')
            % signature_certificate_uuid
        )

    if cert.format not in CERTIFICATE_FORMATS:
        raise exception.SignatureVerificationError(
            _('Invalid certificate format: %s') % cert.format
        )

    if cert.format == X_509:
        # castellan always encodes certificates in DER format
        cert_data = cert.get_encoded()
        certificate = x509.load_der_x509_certificate(cert_data,
                                                     default_backend())
    else:
        raise exception.SignatureVerificationError(
            _('Certificate format not supported: %s') % cert.format
        )

    # verify the certificate
    verify_certificate(certificate)

    return certificate


def verify_certificate(certificate):
    """Verify that the certificate has not expired.

    :param certificate: the cryptography certificate object
    :raises glance.common.exception.SignatureVerificationError: if the
            certificate valid time range does not include now
    """
    # Get now in UTC, since certificate returns times in UTC
    now = datetime.datetime.utcnow()

    # Confirm the certificate valid time range includes now
    if now < certificate.not_valid_before:
        raise exception.SignatureVerificationError(
            _('Certificate is not valid before: %s UTC')
            % certificate.not_valid_before
        )
    elif now > certificate.not_valid_after:
        raise exception.SignatureVerificationError(
            _('Certificate is not valid after: %s UTC')
            % certificate.not_valid_after
        )
