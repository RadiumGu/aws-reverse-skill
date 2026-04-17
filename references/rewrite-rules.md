# CFN Rewrite Rules Reference

Rules applied by `scripts/rewrite_cfn.py` in order.

## Rule 1: Account ID

**Trigger**: Any string value containing a 12-digit AWS account ID.

**Before**:
```yaml
Role: arn:aws:iam::123456789012:role/my-role
```

**After**:
```yaml
Role: !Sub 'arn:aws:iam::${AWS::AccountId}:role/my-role'
```

Pass `--account-id 123456789012` to activate.

---

## Rule 2: Region

**Trigger**: Any string value that exactly equals a known AWS region code.

**Before**:
```yaml
Region: ap-northeast-1
```

**After**:
```yaml
Region: !Sub '${AWS::Region}'
```

Pass `--source-region ap-northeast-1` to activate.

---

## Rule 3: AMI ID

**Trigger**: String values matching `ami-[0-9a-f]{8,17}` in `ImageId` keys.

**Before**:
```yaml
ImageId: ami-0abcdef1234567890
```

**After** (property):
```yaml
ImageId: !Ref AmiId
```

**After** (added Parameter):
```yaml
Parameters:
  AmiId:
    Type: AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>
    Default: /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64
    Description: AMI ID (original: ami-0abcdef1234567890)
```

---

## Rule 4: Availability Zone

**Trigger**: String values matching known AZ pattern (`<region>[a-f]`).

**Before**:
```yaml
AvailabilityZone: ap-northeast-1a
```

**After**:
```yaml
AvailabilityZone: !Select [0, !GetAZs '']
```

> Note: All AZs in a template are mapped to index 0/1/2 of the target region.

---

## Rule 5: DeletionPolicy

**Trigger**: Resource types in the retain list (RDS, S3, DynamoDB, EFS).

**Before**:
```yaml
MyDatabase:
  Type: AWS::RDS::DBInstance
  Properties: ...
```

**After**:
```yaml
MyDatabase:
  Type: AWS::RDS::DBInstance
  DeletionPolicy: Retain
  Properties: ...
```

---

## Out of Scope (Phase 3+)

- KMS CMK ARN → Parameter
- IAM Role ARN cross-account references → Parameter
- Custom Resources
- SSM parameter cross-account references
